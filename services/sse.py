"""
SSE 流式搜索

学 yingshi.xyz 的核心做法：搜到一个推一个，不等全部完成。

我们有两个源：
  showpaw  —— 快（2-4 秒），负责元数据和前期结果
  PanSou   —— 慢（10-20 秒），但量大

改成流式后：
  0.5s   先把页面骨架和「搜索中」推下去
  2-4s   showpaw 的结果先出来，用户就能点了
  15-20s PanSou 的结果陆续补进来

体感从「等 16 秒」变成「2 秒就有东西看」。
"""
import json
import threading
import time

from flask import Response, stream_with_context

# 注意：不能带 Connection，它是 hop-by-hop 头，WSGI 会直接报错
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def sse_event(data, event=None):
    """拼一条 SSE 消息"""
    lines = []
    if event:
        lines.append("event: %s" % event)
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    for ln in payload.split("\n"):
        lines.append("data: %s" % ln)
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def make_stream(search_fn):
    """
    search_fn(kw, emit) -> None
        emit(dict)  推送一条结果
    返回 Flask Response
    """
    def gen():
        started = time.time()
        # 先推一个 meta，让前端立刻知道连上了
        yield sse_event({"type": "start", "t": 0}, "meta")
        try:
            for ev in search_fn():
                if ev is None:
                    continue
                ev["t"] = round(time.time() - started, 2)
                yield sse_event(ev, ev.get("type") or "item")
        except GeneratorExit:
            return
        except Exception as e:
            yield sse_event({"type": "error", "message": str(e)[:160]}, "error")
        finally:
            yield sse_event({"type": "done", "t": round(time.time() - started, 2)}, "done")

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers=SSE_HEADERS)
