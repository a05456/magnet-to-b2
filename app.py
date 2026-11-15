import os
import re
import time
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone

import gradio as gr
import libtorrent as lt
from b2sdk.v2 import InMemoryAccountInfo, B2Api

# מעקב משימות בזיכרון: {job_id: {...}}
JOBS = {}
JOBS_LOCK = threading.Lock()
JOBS_MAX = 200


def job_update(job_id, **fields):
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {"id": job_id})
        job.update(fields)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        # הגבלת גודל — מוחק את הMost ישנות
        if len(JOBS) > JOBS_MAX:
            oldest = sorted(JOBS.values(), key=lambda j: j.get("created_at", ""))
            for j in oldest[: len(JOBS) - JOBS_MAX]:
                JOBS.pop(j["id"], None)


def job_get(job_id):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))


def process_magnet_to_b2(magnet_uri, job_id="", progress=gr.Progress()):
    job_id = re.sub(r"[^\w-]", "", (job_id or "").strip())[:64] or uuid.uuid4().hex[:10]
    job_update(job_id, stage="started", progress=0.0, created_at=datetime.now(timezone.utc).isoformat())

    key_id = os.environ.get("B2_APPLICATION_KEY_ID")
    app_key = os.environ.get("B2_APPLICATION_KEY")
    bucket_name = os.environ.get("B2_BUCKET")

    if not all([key_id, app_key, bucket_name]):
        job_update(job_id, stage="error", error="missing B2 credentials")
        return "❌ שגיאה: חסרים פרטי התחברות ל-B2 (B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET) בהגדרות ה-Environment Variables."

    if not magnet_uri or not magnet_uri.strip().startswith("magnet:?"):
        job_update(job_id, stage="error", error="invalid magnet uri")
        return "❌ שגיאה: אנא הזן קישור magnet תקין."

    logs = []
    def log(msg):
        logs.append(msg)
        print(msg, flush=True)
        text = "\n".join(logs)
        job_update(job_id, logs_text=text)
        return text

    temp_dir = tempfile.mkdtemp()
    ses = None

    try:
        yield log(f"🆔 מזהה משימה: {job_id}")
        yield log("🚀 מתחיל תהליך...")
        yield log(f"📁 נוצרה תיקייה זמנית: {temp_dir}")
        job_update(job_id, stage="downloading", name=magnet_uri.strip()[:80])

        # שרת Kamatera — 4GB RAM, אין צורך בהגבלות של Render.
        # הגבלות קלות בלבד כדי לא לאכול את כל הזיכרון על torrents גדולים
        ses = lt.session({
            "connections_limit": 200,
            "active_downloads": 4,
            "active_seeds": 2,
            "max_queued_disk_bytes": 32 * 1024 * 1024,  # 32 MB disk cache
            "file_pool_size": 64,
            "enable_upnp": False,
            "enable_natpmp": False,
            "listen_interfaces": "0.0.0.0:0",
        })
        params = {
            "save_path": temp_dir,
            "url": magnet_uri.strip(),
        }
        handle = ses.add_torrent(params)
        handle.set_sequential_download(True)

        yield log("⏳ מחפש metadata עבור הקישור...")
        progress(0, desc="מחפש metadata...")
        job_update(job_id, stage="fetching_metadata")

        timeout = 120
        start_wait = time.time()
        while not handle.has_metadata():
            if time.time() - start_wait > timeout:
                raise TimeoutError("עבר הזמן המוקצב למציאת metadata. ייתכן שאין מקורות (Seeders) לקובץ.")
            time.sleep(1)

        torrent_info = handle.get_torrent_info()
        total_mb = torrent_info.total_size() / (1024 * 1024)
        yield log(f"✅ Metadata התקבל! שם: {torrent_info.name()} | גודל: {total_mb:.2f} MB")
        job_update(job_id, stage="downloading", name=torrent_info.name(), size_mb=round(total_mb, 2))

        last_pct = -1
        while not handle.is_seed():
            s = handle.status()
            pct = s.progress * 100
            rate_kb = s.download_rate / 1024
            progress(s.progress, desc=f"מוריד: {pct:.1f}% ({rate_kb:.1f} KB/s)")
            job_update(job_id, progress=round(s.progress, 4), speed_kb_s=round(rate_kb, 1), stage="downloading")
            int_pct = int(pct)
            if int_pct != last_pct:
                yield log(f"⬇️ מוריד: {int_pct}% ({rate_kb:.1f} KB/s)")
                last_pct = int_pct
            time.sleep(1.5)

        yield log("🎉 ההורדה הסתיימה בהצלחה!")
        ses.pause()

        all_files = []
        for root, _, files in os.walk(temp_dir):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, temp_dir).replace("\\", "/")
                all_files.append((full_path, rel_path))

        if not all_files:
            raise RuntimeError("התיקייה ריקה לאחר ההורדה.")

        yield log(f"☁️ מתחבר ל-Backblaze B2 (דלי: {bucket_name})...")
        progress(0, desc="מתחבר ל-B2...")

        info = InMemoryAccountInfo()
        b2_api = B2Api(info)
        b2_api.authorize_account("production", key_id, app_key)
        bucket = b2_api.get_bucket_by_name(bucket_name)

        total_files = len(all_files)
        yield log(f"⬆️ נמצאו {total_files} קבצים להעלאה...")
        job_update(job_id, stage="uploading", files_total=total_files, files_done=0, progress=0.0)

        for idx, (full_path, rel_path) in enumerate(all_files, start=1):
            progress((idx - 1) / total_files, desc=f"מעלה ({idx}/{total_files}): {rel_path}")
            yield log(f"⬆️ מעלה: {rel_path}...")
            bucket.upload_local_file(local_file=full_path, file_name=rel_path)
            job_update(job_id, files_done=idx, current_file=rel_path, progress=round(idx / total_files, 4))
            yield log(f"✅ הועלה בהצלחה: {rel_path}")

        progress(1.0, desc="הושלם!")
        job_update(job_id, stage="completed", progress=1.0)
        yield log("==========================================")
        yield log("🎊 התהליך הושלם בהצלחה! כל הקבצים הועלו ל-B2.")

    except Exception as e:
        job_update(job_id, stage="error", error=str(e))
        yield log(f"❌ אירעה שגיאה: {str(e)}")

    finally:
        if ses:
            try:
                ses.pause()
            except Exception:
                pass
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            yield log("🗑️ התיקייה הזמנית נמחקה מהשרת.")


with gr.Blocks(title="Magnet to Backblaze B2") as demo:
    gr.Markdown("# 🧲 העברת קבצים מ-Magnet אל Backblaze B2")
    gr.Markdown("הדבק קישור Magnet מטה. המערכת תוריד את הקבצים לשרת זמני ותעלה אותם ישירות לדלי (Bucket) שלך ב-B2.")

    with gr.Row():
        magnet_input = gr.Textbox(
            label="קישור Magnet",
            placeholder="magnet:?xt=urn:btih:...",
            lines=2
        )

    job_id_input = gr.Textbox(visible=False)

    submit_btn = gr.Button("התחל הורדה והעלאה", variant="primary")
    output_log = gr.Textbox(label="יומן פעילות (Logs)", lines=15, interactive=False)

    submit_btn.click(
        fn=process_magnet_to_b2,
        inputs=[magnet_input, job_id_input],
        outputs=[output_log]
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    # launch() מחזיר את ה-FastAPI app — מוסיפים אליו routes משלנו
    app, _, _ = demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        prevent_thread_lock=True,
    )

    @app.get("/status")
    def status(job_id: str = ""):
        if not job_id:
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: j.get("created_at", ""), reverse=True)[:20]
            return {"jobs": [{k: v for k, v in j.items() if k != "created_at"} for j in jobs]}
        job = job_get(job_id)
        if not job:
            return {"error": "job not found", "id": job_id}
        return job

    @app.get("/health")
    def health():
        return {"ok": True}

    # השרת רץ ב-thread — משאירים את ה-process חי
    while True:
        time.sleep(3600)
