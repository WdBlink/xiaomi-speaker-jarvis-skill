"""Persist an audio request before sending it from a trusted adapter."""
from contextlib import closing
import json
import sqlite3

from .speaker import MAX_TEXT_LENGTH, SpeakerError


def deliver_speech(db_path, speaker, receipt):
    stopping = any(item["intent"] == "stop_speech" for item in receipt["results"])
    operation = "pause" if stopping else "tts"
    key = (receipt["session_id"], receipt["utterance_id"])
    pending = {"status": "uncertain", "operation": operation,
               "message": "播报请求已登记，发送结果未确认；不会自动重试"}
    try:
        with closing(sqlite3.connect(db_path)) as db:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS cli_speech (
                    session_id TEXT NOT NULL, utterance_id TEXT NOT NULL, result_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, utterance_id))""")
                if not stopping and db.execute(
                        "SELECT 1 FROM home_outbox WHERE session_id=? AND utterance_id=? "
                        "AND state IN ('pending', 'in_flight') LIMIT 1", key).fetchone():
                    return {"status": "deferred", "operation": operation,
                            "message": "家居动作仍在执行，未发送临时回执的播报"}
                saved = db.execute("SELECT receipt FROM submissions WHERE session_id=? AND utterance_id=?", key).fetchone()
                if saved:
                    receipt = json.loads(saved[0])
                if receipt.get("status") == "model_pending":
                    return {"status": "deferred", "operation": operation,
                            "message": "模型仍在处理，未预约最终播报"}
                if receipt.get("status") == "model_cancelled":
                    return {"status": "suppressed", "operation": operation,
                            "message": "旧模型请求已被后续输入替代，未播报"}
                speech_text = "。".join(item["message"].rstrip("。") for item in receipt["results"])
                if len(speech_text) > MAX_TEXT_LENGTH:
                    labels = {"applied": "已确认完成", "failed": "失败", "uncertain": "结果不确定"}
                    results = receipt["results"]
                    if all(item["intent"] == "home" and item.get("status") in labels for item in results):
                        counts = {status: sum(item["status"] == status for item in results) for status in labels}
                        summary = "，".join(f"{count}个{labels[status]}" for status, count in counts.items() if count)
                        speech_text = f"本次{len(results)}个家居动作，{summary}。"
                        if counts["failed"] or counts["uncertain"]:
                            speech_text += "不会自动重试。"
                if not stopping and (not speech_text.strip() or len(speech_text) > MAX_TEXT_LENGTH):
                    return {"status": "rejected", "operation": operation,
                            "message": "回复长度不适合音箱播报，执行回执已保留"}
                claimed = db.execute("INSERT OR IGNORE INTO cli_speech VALUES (?, ?, ?)",
                                     (*key, json.dumps(pending, ensure_ascii=False))).rowcount
            if not claimed:
                stored = db.execute("SELECT result_json FROM cli_speech WHERE session_id=? AND utterance_id=?", key).fetchone()
                return {**json.loads(stored[0]), "replayed": True}
            try:
                status = speaker.stop() if stopping else speaker.speak(speech_text)
                result = {"status": status, "operation": operation}
            except (SpeakerError, ValueError) as error:
                result = {"status": "uncertain", "operation": operation, "message": str(error)}
            with db:
                db.execute("UPDATE cli_speech SET result_json=? WHERE session_id=? AND utterance_id=?",
                           (json.dumps(result, ensure_ascii=False), *key))
            return result
    except sqlite3.Error:
        return {"status": "uncertain", "operation": operation,
                "message": "无法确认播报登记或接收结果；家居回执保留，未自动重试"}
