"""时间轴规划：决定每一句从哪里开口、要不要压语速。

从 ``render.py`` 的对齐主循环里抽出来，做成**纯函数**——它原来埋在 300 行的
``main()`` 里，既没法测也没法复用。抽出后逻辑就一处改动：**槽位按实际开口
时刻算，不按原片时刻算**（见下）。

三条规则，都是踩过坑才定下来的：

1. **可用时长 = 到下一句开口之前**，不是自己的 ``end``。
   句间停顿也是可用时间，这一条让很多本来要压语速的句子不用压。
2. **槽位按实际开口时刻算**。原先写的是 ``slot = next_ideal - ideal``，
   于是一句晚了 3 秒开口，它仍然按「自己的理想槽位」算长度，于是必然又
   越过下一句的起点——延迟自我放大。改成 ``next_ideal - start`` 之后，
   晚开口的句子会自动多压一点，把欠的时间当场还掉。
   实测：30 分钟样本上平均漂移 0.99s → 0.69s，超过 1 秒的句子 36 → 28。
3. **首句保护**（``head_clamped``）：整轨前移时首句会顶到 0 之前，
   此时不能压它的语速——那会让开场比全片快一截，听起来像赶。

还有一个容易走偏的地方：**排布救不了译文太长**。
同一个样本里有 35 句即使按 1.15 倍速上限也塞不进槽位，其中最长的一句
语音 40 秒、槽位只有 8 秒——怎么排都会漂 16 秒。真正的解法在翻译阶段：
``units.audit_budget`` 会把这些句子挑出来按字数上限重译。所以渲染报告里
``overflowed`` 是比 ``drift_max`` 更该看的指标。
"""

from __future__ import annotations

from dataclasses import dataclass

HEAD_EPS = 0.01       # 早于 -0.01 秒算「顶到 0 之前」
TEMPO_EPS = 0.02      # 超出槽位 2% 以上才压，避免为了几十毫秒去动语速


@dataclass
class Placement:
    index: int
    target: float          # 按原时间轴该开口的时刻（含 offset）
    start_at: float        # 实际开口时刻
    slot: float            # 可用时长
    raw_dur: float         # 未压缩的语音时长
    clip_dur: float        # 压缩后的时长
    tempo: float
    head_clamped: bool
    overflowed: bool       # 压到上限还塞不下
    borrowed: float        # 为了塞进去而提前开口的秒数
    drift: float           # start_at - target，正数=落后于原声

    @property
    def compressed(self) -> bool:
        return self.tempo > 1.0 + 1e-6


@dataclass
class AlignmentStats:
    placed: int = 0
    tempo_compressed: int = 0
    tempo_avg: float = 1.0
    tempo_p95: float = 1.0
    tempo_max: float = 1.0
    drift_max: float = 0.0         # 最大落后量
    drift_early: float = 0.0       # 最大提前量
    drift_mean: float = 0.0        # 平均偏移绝对值
    drift_over_1s: int = 0         # 偏移超过 1 秒的句数
    overflowed: int = 0
    borrowed_total: float = 0.0
    final_cursor: float = 0.0
    total: float = 0.0

    @property
    def tail_silence(self) -> float:
        return max(self.total - self.final_cursor, 0.0)

    def to_dict(self) -> dict:
        return {
            "placed": self.placed,
            "tempo_compressed": self.tempo_compressed,
            "tempo_avg": round(self.tempo_avg, 3),
            "tempo_p95": round(self.tempo_p95, 3),
            "tempo_max": round(self.tempo_max, 3),
            "drift_max": round(self.drift_max, 2),
            "drift_early": round(self.drift_early, 2),
            "drift_mean": round(self.drift_mean, 2),
            "drift_over_1s": self.drift_over_1s,
            "overflowed": self.overflowed,
            "borrowed_total": round(self.borrowed_total, 2),
            "final_cursor": round(self.final_cursor, 2),
            "total": round(self.total, 2),
            "tail_silence": round(self.tail_silence, 2),
        }


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 1.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * q), len(ordered) - 1)
    return ordered[idx]


def plan_placement(
    items: list[dict],
    durations: list[float],
    total: float = 0.0,
    *,
    offset: float = 0.0,
    max_tempo: float = 1.15,
    guard: float = 0.06,
    min_slot: float = 0.5,
    early_max: float = 0.35,
) -> tuple[list[Placement], AlignmentStats]:
    """为每一句算出开口时刻与语速。

    参数
    ----
    items       含 ``start`` 的对话条目（已按时间排序）
    durations   与 items 等长的语音原始时长（秒，需先裁静音）
    total       视频总时长；<=0 时用最后一句的 end 兜底
    offset      整条音轨平移量（负值 = 提前），用于补偿源文件音画错位
    max_tempo   单句语速压缩上限，越小越自然、代价是更多顺延
    guard       句间留缝，避免两句听起来连成一句
    early_max   装不下时最多提前多少秒开口（借上一句留下的空隙）

    返回 ``(placements, stats)``。纯函数，不碰 IO，可直接单测。
    """
    if not items:
        return [], AlignmentStats(total=max(total, 0.0))

    durations = list(durations[: len(items)])
    while len(durations) < len(items):
        durations.append(0.0)

    if total <= 0:
        last = items[-1]
        total = max(float(last.get("end", last["start"])) + offset,
                    float(last["start"]) + offset)

    placements: list[Placement] = []
    tempos: list[float] = []
    drifts: list[float] = []
    borrowed_total = 0.0
    cursor = 0.0          # 上一句结束时刻

    n = len(items)
    for i in range(n):
        raw_start = float(items[i]["start"]) + offset
        head_clamped = raw_start < -HEAD_EPS
        ideal = max(raw_start, 0.0)
        nxt = float(items[i + 1]["start"]) + offset if i + 1 < n else 0.0
        next_ideal = nxt if nxt > 0 else max(total, ideal)

        raw = durations[i]
        # ---- 1. 定开口时刻
        # 按上限语速也塞不进理想槽位时，往前借一段时间（但不能压到上一句身上）
        latest = next_ideal - guard - raw / max_tempo
        start = max(min(ideal, latest), ideal - early_max, cursor)
        if head_clamped:
            start = max(start, 0.0)
        borrowed = max(ideal - start, 0.0)
        borrowed_total += borrowed

        # ---- 2. 定槽位：从实际开口时刻算起
        slot = max(next_ideal - start - guard, min_slot)
        if head_clamped:
            slot = max(slot, raw)      # 首句不许压语速

        # ---- 3. 定语速
        tempo = 1.0
        clip_dur = raw
        overflowed = False
        if raw > slot * (1.0 + TEMPO_EPS):
            ratio = raw / slot
            tempo = min(ratio, max_tempo)
            clip_dur = raw / tempo
            overflowed = ratio > max_tempo + 1e-9

        cursor = start + clip_dur
        drift = start - ideal

        placements.append(Placement(
            index=i, target=round(ideal, 4), start_at=round(start, 4),
            slot=round(slot, 3), raw_dur=round(raw, 3),
            clip_dur=round(clip_dur, 3), tempo=round(tempo, 4),
            head_clamped=head_clamped, overflowed=overflowed,
            borrowed=round(borrowed, 3), drift=round(drift, 3),
        ))
        if tempo > 1.0 + 1e-6:
            tempos.append(tempo)
        drifts.append(drift)

    lateness = [max(d, 0.0) for d in drifts]
    earliness = [max(-d, 0.0) for d in drifts]
    absd = [abs(d) for d in drifts]
    stats = AlignmentStats(
        placed=len(placements),
        tempo_compressed=len(tempos),
        tempo_avg=round(sum(tempos) / len(tempos), 3) if tempos else 1.0,
        tempo_p95=round(_pct(tempos, 0.95), 3),
        tempo_max=round(max(tempos), 3) if tempos else 1.0,
        drift_max=round(max(lateness), 3),
        drift_early=round(max(earliness), 3),
        drift_mean=round(sum(absd) / len(absd), 3),
        drift_over_1s=sum(1 for d in absd if d > 1.0),
        overflowed=sum(1 for p in placements if p.overflowed),
        borrowed_total=round(borrowed_total, 2),
        final_cursor=round(cursor, 2),
        total=round(total, 2),
    )
    return placements, stats
