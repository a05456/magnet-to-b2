import os
import time
import shutil
import tempfile

import gradio as gr
import libtorrent as lt
from b2sdk.v2 import InMemoryAccountInfo, B2Api


def process_magnet_to_b2(magnet_uri, progress=gr.Progress()):
    key_id = os.environ.get("B2_APPLICATION_KEY_ID")
    app_key = os.environ.get("B2_APPLICATION_KEY")
    bucket_name = os.environ.get("B2_BUCKET")

    if not all([key_id, app_key, bucket_name]):
        return "❌ שגיאה: חסרים פרטי התחברות ל-B2 (B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET) בהגדרות ה-Environment Variables."

    if not magnet_uri or not magnet_uri.strip().startswith("magnet:?"):
        return "❌ שגיאה: אנא הזן קישור magnet תקין."

    logs = []
    def log(msg):
        logs.append(msg)
        print(msg, flush=True)
        return "\n".join(logs)

    temp_dir = tempfile.mkdtemp()
    ses = None

    try:
        yield log("🚀 מתחיל תהליך...")
        yield log(f"📁 נוצרה תיקייה זמנית: {temp_dir}")

        ses = lt.session()
        params = {
            "save_path": temp_dir,
            "url": magnet_uri.strip(),
        }
        handle = ses.add_torrent(params)
        handle.set_sequential_download(True)

        yield log("⏳ מחפש metadata עבור הקישור...")
        progress(0, desc="מחפש metadata...")

        timeout = 120
        start_wait = time.time()
        while not handle.has_metadata():
            if time.time() - start_wait > timeout:
                raise TimeoutError("עבר הזמן המוקצב למציאת metadata. ייתכן שאין מקורות (Seeders) לקובץ.")
            time.sleep(1)

        torrent_info = handle.get_torrent_info()
        total_mb = torrent_info.total_size() / (1024 * 1024)
        yield log(f"✅ Metadata התקבל! שם: {torrent_info.name()} | גודל: {total_mb:.2f} MB")

        last_pct = -1
        while not handle.is_seed():
            s = handle.status()
            pct = s.progress * 100
            rate_kb = s.download_rate / 1024
            progress(s.progress, desc=f"מוריד: {pct:.1f}% ({rate_kb:.1f} KB/s)")
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

        for idx, (full_path, rel_path) in enumerate(all_files, start=1):
            progress((idx - 1) / total_files, desc=f"מעלה ({idx}/{total_files}): {rel_path}")
            yield log(f"⬆️ מעלה: {rel_path}...")
            bucket.upload_local_file(local_file=full_path, file_name=rel_path)
            yield log(f"✅ הועלה בהצלחה: {rel_path}")

        progress(1.0, desc="הושלם!")
        yield log("==========================================")
        yield log("🎊 התהליך הושלם בהצלחה! כל הקבצים הועלו ל-B2.")

    except Exception as e:
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

    submit_btn = gr.Button("התחל הורדה והעלאה", variant="primary")
    output_log = gr.Textbox(label="יומן פעילות (Logs)", lines=15, interactive=False)

    submit_btn.click(
        fn=process_magnet_to_b2,
        inputs=[magnet_input],
        outputs=[output_log]
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    demo.launch(server_name="0.0.0.0", server_port=port)
