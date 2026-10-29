// SSE 封装：一种事件类型一个监听，收到终态事件主动关闭
// （EventSource 在服务端断开时会自动重连，不关的话会无限重连）

const KINDS = ["progress", "stage", "log", "done", "error"];

export class JobStream {
  constructor(jobId, { onEvent, onClose } = {}) {
    this.jobId = jobId;
    this.onEvent = onEvent;
    this.onClose = onClose;
    this.lastSeq = 0;
    this.closed = false;
    this.src = null;
  }

  connect() {
    if (this.closed) return this;
    try {
      this.src = new EventSource(`/api/jobs/${this.jobId}/events`);
    } catch {
      return this;
    }

    for (const kind of KINDS) {
      this.src.addEventListener(kind, (e) => this._handle(kind, e));
    }

    this.src.onerror = () => {
      // 正常断线交给浏览器重连；已终止则什么都不做
      if (this.closed) this.src?.close();
    };
    return this;
  }

  _handle(kind, e) {
    let data;
    try {
      data = JSON.parse(e.data);
    } catch {
      return;
    }
    if (data.seq) this.lastSeq = data.seq;
    this.onEvent?.(data);
    if (kind === "done" || kind === "error") this.close();
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.src?.close();
    this.src = null;
    this.onClose?.(this.jobId);
  }
}

export class StreamHub {
  constructor(opts) {
    this.opts = opts;
    this.map = new Map();
  }

  ensure(jobId) {
    if (this.map.has(jobId)) return;
    const s = new JobStream(jobId, {
      onEvent: (ev) => this.opts.onEvent?.(jobId, ev),
      onClose: (id) => {
        this.map.delete(id);
        this.opts.onClose?.(id);
      },
    });
    this.map.set(jobId, s);
    s.connect();
  }

  drop(jobId) {
    this.map.get(jobId)?.close();
    this.map.delete(jobId);
  }

  clear() {
    for (const s of this.map.values()) s.close();
    this.map.clear();
  }
}
