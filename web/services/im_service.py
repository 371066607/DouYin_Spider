from datetime import datetime, timezone
import json

from dy_apis.douyin_api import DouyinAPI
from dy_apis.douyin_recv_msg import DouyinRecvMsg
from web.db import connect_db, init_db

UTC = timezone.utc


class IMService:
    def __init__(self, db_path, session_service, task_manager, broker, receiver_cls=DouyinRecvMsg):
        self.db_path = db_path
        self.sessions = session_service
        self.task_manager = task_manager
        self.broker = broker
        self.receiver_cls = receiver_cls
        with connect_db(self.db_path) as conn:
            init_db(conn)

    def _auth(self):
        auth = self.sessions.load_auth("douyin")
        if auth is None:
            raise RuntimeError("Missing douyin cookie")
        return auth

    def create_conversation(self, to_user_id):
        conversation_id, conversation_short_id, ticket = DouyinAPI.create_conversation(self._auth(), int(to_user_id))
        return {
            "conversation_id": conversation_id,
            "conversation_short_id": conversation_short_id,
            "ticket": ticket,
        }

    def get_conversation_detail(self, to_user_id, conversation_short_id):
        payload = DouyinAPI.get_conversation_list(self._auth(), int(to_user_id), int(conversation_short_id))
        return {"detail": payload}

    def send_message(self, conversation_id, conversation_short_id, ticket, content):
        payload = DouyinAPI.send_msg(self._auth(), conversation_id, conversation_short_id, ticket, content)
        return {"detail": payload}

    def start_receiver(self):
        def sink(payload):
            event = {"channel": "im", "payload": payload}
            self._record_event(event)
            self.broker.publish("events", event)

        runtime = self.receiver_cls(
            self._auth(),
            auto_reconnect=True,
            event_sink=sink,
            error_sink=lambda err: sink({"event_type": "error", "error": str(err)}),
            close_sink=lambda payload: sink({"event_type": "closed", "payload": payload}),
        )
        self.task_manager.runtimes["im:default"] = runtime
        with connect_db(self.db_path) as conn:
            conn.execute(
                "insert into im_receivers(scope, status, started_at, stopped_at, last_error) values(?, ?, ?, ?, ?) "
                "on conflict(scope) do update set status=excluded.status, started_at=excluded.started_at, stopped_at=excluded.stopped_at, last_error=excluded.last_error",
                ("default", "running", datetime.now(UTC).isoformat(), None, ""),
            )
            conn.commit()
        self.task_manager.submit("im.receive", "default", runtime.start)

    def _record_event(self, event):
        payload = event.get("payload") or {}
        event_type = str(payload.get("event_type") or "im")
        with connect_db(self.db_path) as conn:
            conn.execute(
                "insert into event_feed(channel, event_type, payload, created_at) values(?, ?, ?, ?)",
                ("im", event_type, json.dumps(event, ensure_ascii=False), datetime.now(UTC).isoformat()),
            )
            conn.commit()

    # ---- 实时私信：从 event_feed 读取会话与消息，供桌面聊天界面使用 ----

    MSG_TYPES = {"text", "emoji", "voice", "image", "share"}

    @staticmethod
    def _preview(inner):
        et = inner.get("event_type")
        if et == "text":
            return str(inner.get("content") or "")
        return {
            "emoji": "[表情]", "voice": "[语音]", "image": "[图片]", "share": "[分享作品]",
        }.get(et, f"[{et}]")

    def _iter_im_events(self, order, limit):
        with connect_db(self.db_path) as conn:
            cur = conn.execute(
                f"select payload, created_at from event_feed where channel='im' "
                f"order by id {order} limit ?",
                (limit,),
            )
            rows = cur.fetchall()
        for r in rows:
            try:
                inner = (json.loads(r["payload"]).get("payload")) or {}
            except Exception:
                continue
            if inner.get("event_type") not in self.MSG_TYPES:
                continue
            yield inner, r["created_at"]

    def _nickname_map(self, uids):
        """从已采集的评论/视频数据反查 uid→昵称（免费，不额外请求接口）。"""
        uids = [u for u in {str(x or "") for x in uids} if u]
        if not uids:
            return {}
        placeholders = ",".join("?" * len(uids))
        out = {}
        with connect_db(self.db_path) as conn:
            for table in ("agent_comment_items", "agent_video_items"):
                try:
                    rows = conn.execute(
                        f"select user_id, nickname from {table} "
                        f"where user_id in ({placeholders}) and nickname <> ''",
                        tuple(uids),
                    ).fetchall()
                    for r in rows:
                        out.setdefault(str(r["user_id"]), r["nickname"])
                except Exception:
                    continue
        return out

    def list_conversations(self, limit=300):
        """按会话聚合：返回 [{conversation_id, sender, nickname, preview, last_time, count}]，最新在前。"""
        convs = {}
        for inner, created_at in self._iter_im_events("desc", 5000):
            cid = str(inner.get("conversation_id") or "")
            if not cid:
                continue
            if cid not in convs:
                convs[cid] = {
                    "conversation_id": cid,
                    "sender": str(inner.get("sender") or ""),
                    "preview": self._preview(inner),
                    "last_time": created_at,
                    "count": 0,
                }
            convs[cid]["count"] += 1
        out = sorted(convs.values(), key=lambda x: x["last_time"], reverse=True)[:limit]
        nmap = self._nickname_map([c["sender"] for c in out])
        for c in out:
            c["nickname"] = nmap.get(c["sender"], "")
        return out

    def list_messages(self, conversation_id, limit=500):
        """某会话的消息流：返回 [{sender, type, text, time}]，最旧在前。"""
        cid = str(conversation_id or "")
        msgs = []
        for inner, created_at in self._iter_im_events("asc", 20000):
            if str(inner.get("conversation_id") or "") != cid:
                continue
            msgs.append({
                "sender": str(inner.get("sender") or ""),
                "type": inner.get("event_type"),
                "text": self._preview(inner),
                "time": created_at,
            })
        return msgs[-limit:]

    def receiver_running(self):
        return "im:default" in self.task_manager.runtimes

    def stop_receiver(self):
        runtime = self.task_manager.runtimes.pop("im:default", None)
        if runtime:
            runtime.stop()
        with connect_db(self.db_path) as conn:
            conn.execute(
                "update im_receivers set status = ?, stopped_at = ? where scope = ?",
                ("stopped", datetime.now(UTC).isoformat(), "default"),
            )
            conn.commit()
