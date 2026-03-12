import asyncio
import base64
import json
import re
import shutil
import time
from pathlib import Path

import httpx
import streamlit as st
from pydub import AudioSegment
from streamlit_local_storage import LocalStorage


# =========================
# PATH / CONFIG
# =========================
APP_DIR = Path(".").resolve()
OUTPUT_DIR = APP_DIR / "tts_output"
SENTENCES_DIR = OUTPUT_DIR / "sentences"
SENTENCES_META_FILE = OUTPUT_DIR / "sentences.json"

PROXY_API_URL = "https://unendowed-jacquelyn-guilefully.ngrok-free.dev/v1/tts"

DEFAULT_VOICES = {
    "Mặc định": "hruBcESGYx2AUWRppNacCd"
}

DEFAULT_SETTINGS = {
    "proxy_api_key": "",
    "speed": "1.0",
    "pause_ms": "300",
    "output_name": "final_output.mp3",
    "selected_voice_name": "Mặc định",
    "project_name": "default",
    "main_text": "",
    "input_mode": "text",
}

MAX_CONCURRENT = 5
BLOCK_VERSION = 0

LS_PREFIX = "lucylab_tts"


# =========================
# LOCAL STORAGE HELPERS
# =========================
def ls_key(name: str) -> str:
    return f"{LS_PREFIX}:{name}"


def load_json_from_ls(localS: LocalStorage, key: str, default):
    try:
        raw = localS.getItem(ls_key(key))
        if raw in (None, "", "null", "undefined"):
            return default
        return json.loads(raw)
    except Exception:
        return default


def save_json_to_ls(localS: LocalStorage, key: str, value):
    try:
        localS.setItem(ls_key(key), json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


def load_str_from_ls(localS: LocalStorage, key: str, default: str = "") -> str:
    try:
        val = localS.getItem(ls_key(key))
        if val in (None, "null", "undefined"):
            return default
        return str(val)
    except Exception:
        return default


def save_str_to_ls(localS: LocalStorage, key: str, value: str):
    try:
        localS.setItem(ls_key(key), value if value is not None else "")
    except Exception:
        pass


def load_browser_state(localS: LocalStorage):
    data = {
        "project_name": load_str_from_ls(localS, "project_name", DEFAULT_SETTINGS["project_name"]),
        "proxy_api_key": load_str_from_ls(localS, "proxy_api_key", DEFAULT_SETTINGS["proxy_api_key"]),
        "speed": load_str_from_ls(localS, "speed", DEFAULT_SETTINGS["speed"]),
        "pause_ms": load_str_from_ls(localS, "pause_ms", DEFAULT_SETTINGS["pause_ms"]),
        "output_name": load_str_from_ls(localS, "output_name", DEFAULT_SETTINGS["output_name"]),
        "selected_voice_name": load_str_from_ls(localS, "selected_voice_name", DEFAULT_SETTINGS["selected_voice_name"]),
        "main_text": load_str_from_ls(localS, "main_text", DEFAULT_SETTINGS["main_text"]),
        "input_mode": load_str_from_ls(localS, "input_mode", DEFAULT_SETTINGS["input_mode"]),
        "voices": load_json_from_ls(localS, "voices", DEFAULT_VOICES.copy()),
    }

    if not isinstance(data["voices"], dict) or not data["voices"]:
        data["voices"] = DEFAULT_VOICES.copy()

    return data


def save_browser_state(localS: LocalStorage):
    save_str_to_ls(localS, "project_name", st.session_state.get("project_name", "default"))
    save_str_to_ls(localS, "proxy_api_key", st.session_state.get("proxy_api_key", ""))
    save_str_to_ls(localS, "speed", st.session_state.get("speed", DEFAULT_SETTINGS["speed"]))
    save_str_to_ls(localS, "pause_ms", st.session_state.get("pause_ms", DEFAULT_SETTINGS["pause_ms"]))
    save_str_to_ls(localS, "output_name", st.session_state.get("output_name", DEFAULT_SETTINGS["output_name"]))
    save_str_to_ls(localS, "selected_voice_name", st.session_state.get("selected_voice_name", DEFAULT_SETTINGS["selected_voice_name"]))
    save_str_to_ls(localS, "main_text", st.session_state.get("main_text", ""))
    save_str_to_ls(localS, "input_mode", st.session_state.get("input_mode", "text"))
    save_json_to_ls(localS, "voices", st.session_state.get("voices", DEFAULT_VOICES.copy()))


def clear_browser_state(localS: LocalStorage):
    for k in [
        "project_name",
        "proxy_api_key",
        "speed",
        "pause_ms",
        "output_name",
        "selected_voice_name",
        "main_text",
        "input_mode",
        "voices",
    ]:
        try:
            localS.deleteItem(ls_key(k))
        except Exception:
            pass


# =========================
# UTIL
# =========================
def ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SENTENCES_DIR.mkdir(parents=True, exist_ok=True)


def clear_runtime_files():
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    ensure_dirs()


def split_sentences(text: str):
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?…])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def extract_audio_info(data):
    result = data.get("result", data) if isinstance(data, dict) else data

    audio_url = None
    audio_base64 = None

    if isinstance(result, dict):
        audio_url = (
            result.get("url")
            or result.get("audioUrl")
            or result.get("downloadUrl")
            or result.get("fileUrl")
        )
        audio_base64 = (
            result.get("audioBase64")
            or result.get("base64")
            or result.get("audio")
        )

    return audio_url, audio_base64


def detect_extension_from_bytes(audio_bytes: bytes):
    if audio_bytes.startswith(b"RIFF") and b"WAVE" in audio_bytes[:16]:
        return "wav"
    if audio_bytes[:3] == b"ID3" or audio_bytes[:2] == b"\xff\xfb":
        return "mp3"
    if audio_bytes[:4] == b"fLaC":
        return "flac"
    if audio_bytes[:4] == b"OggS":
        return "ogg"
    if len(audio_bytes) >= 8 and audio_bytes[4:8] == b"ftyp":
        return "m4a"
    return "bin"


def sentence_file_prefix(idx: int):
    return f"{idx:03d}"


def find_sentence_audio(idx: int):
    prefix = sentence_file_prefix(idx)
    for ext in ["wav", "mp3", "flac", "ogg", "m4a", "bin"]:
        p = SENTENCES_DIR / f"{prefix}.{ext}"
        if p.exists():
            return p
    return None


def save_sentences_meta(items: list[dict]):
    ensure_dirs()
    SENTENCES_META_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sentences_meta():
    ensure_dirs()
    if SENTENCES_META_FILE.exists():
        try:
            data = json.loads(SENTENCES_META_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def rebuild_full_text_from_meta(meta: list[dict]) -> str:
    ordered = sorted(meta, key=lambda x: x["index"])
    return "\n".join(item["text"] for item in ordered if item.get("text"))


def ms_to_srt_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    hours = ms // 3600000
    ms %= 3600000
    minutes = ms // 60000
    ms %= 60000
    seconds = ms // 1000
    ms %= 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def srt_time_to_ms(s: str) -> int:
    s = s.strip().replace(".", ",")
    m = re.match(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", s)
    if not m:
        raise ValueError(f"Timeline SRT không hợp lệ: {s}")
    hh, mm, ss, ms = map(int, m.groups())
    return (((hh * 60) + mm) * 60 + ss) * 1000 + ms


def parse_srt_blocks(srt_text: str) -> list[dict]:
    srt_text = srt_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not srt_text:
        return []

    chunks = re.split(r"\n\s*\n", srt_text)
    items = []

    for chunk in chunks:
        lines = [line.rstrip() for line in chunk.split("\n") if line.strip() != ""]
        if len(lines) < 2:
            continue

        if "-->" in lines[0]:
            timeline_line = lines[0]
            text_lines = lines[1:]
        else:
            if len(lines) < 3 or "-->" not in lines[1]:
                continue
            timeline_line = lines[1]
            text_lines = lines[2:]

        m = re.match(r"\s*(.*?)\s*-->\s*(.*?)\s*$", timeline_line)
        if not m:
            continue

        start_raw, end_raw = m.groups()
        start_ms = srt_time_to_ms(start_raw)
        end_ms = srt_time_to_ms(end_raw)
        text = " ".join(t.strip() for t in text_lines if t.strip())
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            continue

        items.append({
            "index": len(items) + 1,
            "text": text,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "timeline": f"{ms_to_srt_time(start_ms)} --> {ms_to_srt_time(end_ms)}",
            "source": "srt",
        })

    return items


def build_text_entries(text: str) -> list[dict]:
    sentences = split_sentences(text)
    return [
        {"index": i, "text": s, "source": "text"}
        for i, s in enumerate(sentences, start=1)
    ]


def normalize_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "Không kết nối được tới proxy server."
    if isinstance(exc, httpx.TimeoutException):
        return "Proxy server phản hồi quá lâu (timeout)."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = ""
        try:
            data = exc.response.json()
            if isinstance(data, dict):
                detail = str(data.get("detail", "")).strip()
        except Exception:
            try:
                detail = exc.response.text.strip()
            except Exception:
                detail = ""

        if status == 401:
            return detail or "Thiếu API key."
        if status == 403:
            return detail or "API key không hợp lệ."
        if status == 400:
            return detail or "Request không hợp lệ."
        if status == 500:
            return detail or "Proxy server lỗi cấu hình hoặc lỗi nội bộ."
        if status == 502:
            return detail or "Proxy gọi LucyLab thất bại."
        if status == 504:
            return detail or "Proxy timeout khi gọi LucyLab."
        return detail or f"Lỗi HTTP {status}"
    return str(exc)


# =========================
# ENGINE
# =========================
class TTSEngine:
    def __init__(self):
        ensure_dirs()

    async def download_file_bytes(self, client: httpx.AsyncClient, url: str):
        resp = await client.get(url, timeout=180)
        resp.raise_for_status()
        return resp.content

    async def request_tts_bytes(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        text: str,
        voice_id: str,
        speed: float,
        block_version: int,
    ):
        headers = {
            "accept": "*/*",
            "x-api-key": api_key,
            "content-type": "application/json",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
            ),
        }

        payload = {
            "method": "tts",
            "input": {
                "text": text,
                "userVoiceId": voice_id,
                "speed": speed,
                "blockVersion": block_version
            }
        }

        try:
            resp = await client.post(PROXY_API_URL, headers=headers, json=payload, timeout=180)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RuntimeError(normalize_http_error(e)) from e

        audio_url, audio_base64 = extract_audio_info(data)
        if audio_url:
            return await self.download_file_bytes(client, audio_url), data
        if audio_base64:
            return base64.b64decode(audio_base64), data
        return None, data

    async def tts_one_sentence(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        idx: int,
        sentence: str,
        api_key: str,
        voice_id: str,
        speed: float,
        block_version: int,
    ):
        async with sem:
            prefix = sentence_file_prefix(idx)
            txt_path = SENTENCES_DIR / f"{prefix}.txt"
            raw_json_path = SENTENCES_DIR / f"{prefix}_response.json"
            txt_path.write_text(sentence, encoding="utf-8")

            try:
                audio_bytes, data = await self.request_tts_bytes(
                    client=client,
                    api_key=api_key,
                    text=sentence,
                    voice_id=voice_id,
                    speed=speed,
                    block_version=block_version,
                )

                if not audio_bytes:
                    raw_json_path.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )
                    return None, idx, f"[{prefix}] Không thấy audio trong response"

                ext = detect_extension_from_bytes(audio_bytes)
                audio_path = SENTENCES_DIR / f"{prefix}.{ext}"
                audio_path.write_bytes(audio_bytes)
                return audio_path, idx, f"[{prefix}] OK"

            except Exception as e:
                raw_json_path.write_text(str(e), encoding="utf-8")
                return None, idx, f"[{prefix}] Lỗi: {e}"

    async def run_tts_entries(
        self,
        entries: list[dict],
        api_key: str,
        voice_id: str,
        speed: float,
        pause_ms: int,
        output_name: str,
        block_version: int = 0,
    ):
        ensure_dirs()
        if not entries:
            raise ValueError("Không có câu nào để xử lý.")

        save_sentences_meta(entries)

        sem = asyncio.Semaphore(MAX_CONCURRENT)
        success_count = 0
        logs = []

        async with httpx.AsyncClient(follow_redirects=True) as client:
            tasks = [
                self.tts_one_sentence(
                    client=client,
                    sem=sem,
                    idx=item["index"],
                    sentence=item["text"],
                    api_key=api_key,
                    voice_id=voice_id,
                    speed=speed,
                    block_version=block_version,
                )
                for item in entries
            ]

            total = len(tasks)
            completed = 0
            progress_box = st.progress(0, text="Đang xử lý TTS...")

            for future in asyncio.as_completed(tasks):
                audio_path, idx, log_msg = await future
                completed += 1
                logs.append(log_msg)
                st.session_state["runtime_logs"] = logs.copy()
                progress_box.progress(int(completed * 100 / total), text=f"Đã xong câu {idx}/{total}")

                if audio_path is not None:
                    success_count += 1

        progress_box.empty()

        if success_count == 0:
            raise RuntimeError("Không tạo được audio nào. Kiểm tra lại API key hoặc proxy server.")

        final_path = self.merge_sentences_to_final(
            pause_ms=pause_ms,
            output_name=output_name,
        )
        return final_path, logs

    async def run_tts(
        self,
        text: str,
        api_key: str,
        voice_id: str,
        speed: float,
        pause_ms: int,
        output_name: str,
        block_version: int = 0,
    ):
        entries = build_text_entries(text)
        return await self.run_tts_entries(
            entries=entries,
            api_key=api_key,
            voice_id=voice_id,
            speed=speed,
            pause_ms=pause_ms,
            output_name=output_name,
            block_version=block_version,
        )

    async def run_tts_srt(
        self,
        srt_text: str,
        api_key: str,
        voice_id: str,
        speed: float,
        output_name: str,
        block_version: int = 0,
    ):
        entries = parse_srt_blocks(srt_text)
        return await self.run_tts_entries(
            entries=entries,
            api_key=api_key,
            voice_id=voice_id,
            speed=speed,
            pause_ms=0,
            output_name=output_name,
            block_version=block_version,
        )

    async def regenerate_one_sentence(
        self,
        idx: int,
        sentence: str,
        api_key: str,
        voice_id: str,
        speed: float,
        block_version: int = 0,
    ):
        ensure_dirs()
        prefix = sentence_file_prefix(idx)

        old_audio = find_sentence_audio(idx)
        if old_audio and old_audio.exists():
            last_err = None
            for _ in range(10):
                try:
                    old_audio.unlink()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.15)

            if old_audio.exists():
                raise RuntimeError(f"Không thể xóa file audio cũ đang bị khóa: {old_audio} | {last_err}")

        async with httpx.AsyncClient(follow_redirects=True) as client:
            audio_bytes, data = await self.request_tts_bytes(
                client=client,
                api_key=api_key,
                text=sentence + " ",
                voice_id=voice_id,
                speed=speed,
                block_version=block_version,
            )

            if not audio_bytes:
                raw_json_path = SENTENCES_DIR / f"{prefix}_response.json"
                raw_json_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                raise RuntimeError("API không trả audio cho câu này.")

            ext = detect_extension_from_bytes(audio_bytes)
            audio_path = SENTENCES_DIR / f"{prefix}.{ext}"
            audio_path.write_bytes(audio_bytes)

            txt_path = SENTENCES_DIR / f"{prefix}.txt"
            txt_path.write_text(sentence, encoding="utf-8")
            return audio_path

    def merge_sentences_to_final(self, pause_ms: int, output_name: str):
        ensure_dirs()
        meta = load_sentences_meta()
        if not meta:
            raise RuntimeError("Không có dữ liệu câu để ghép.")

        is_srt_mode = any(("start_ms" in item and "end_ms" in item) for item in meta)

        if is_srt_mode:
            return self.merge_srt_timeline(output_name=output_name)
        return self.merge_sequential(pause_ms=pause_ms, output_name=output_name)

    def merge_sequential(self, pause_ms: int, output_name: str):
        meta = load_sentences_meta()
        final_audio = AudioSegment.empty()
        silence = AudioSegment.silent(duration=max(0, pause_ms))

        available_segments = []
        for item in meta:
            audio_path = find_sentence_audio(item["index"])
            if audio_path:
                available_segments.append((item["index"], audio_path))

        if not available_segments:
            raise RuntimeError("Không có audio câu nào để ghép.")

        for i, (_, audio_path) in enumerate(available_segments, start=1):
            seg = AudioSegment.from_file(audio_path)
            final_audio += seg
            if i < len(available_segments) and pause_ms > 0:
                final_audio += silence

        if not output_name.lower().endswith(".mp3"):
            output_name += ".mp3"

        final_path = OUTPUT_DIR / output_name
        final_audio.export(final_path, format="mp3", bitrate="192k")
        return final_path

    def merge_srt_timeline(self, output_name: str):
        meta = load_sentences_meta()

        segments = []
        max_end = 0

        for item in meta:
            idx = item["index"]
            start_ms = int(item.get("start_ms", 0))
            end_ms = int(item.get("end_ms", start_ms))
            audio_path = find_sentence_audio(idx)

            if not audio_path:
                continue

            seg = AudioSegment.from_file(audio_path)
            segments.append((idx, start_ms, end_ms, seg))

            real_end = max(end_ms, start_ms + len(seg))
            if real_end > max_end:
                max_end = real_end

        if not segments:
            raise RuntimeError("Không có audio câu nào để ghép.")

        final_audio = AudioSegment.silent(duration=max_end + 50)

        for _, start_ms, _, seg in segments:
            final_audio = final_audio.overlay(seg, position=start_ms)

        if not output_name.lower().endswith(".mp3"):
            output_name += ".mp3"

        final_path = OUTPUT_DIR / output_name
        final_audio.export(final_path, format="mp3", bitrate="192k")
        return final_path


# =========================
# STREAMLIT UI
# =========================
ensure_dirs()
localS = LocalStorage()

if "runtime_logs" not in st.session_state:
    st.session_state["runtime_logs"] = []

if "browser_bootstrap_done" not in st.session_state:
    st.session_state["browser_bootstrap_done"] = False

if not st.session_state["browser_bootstrap_done"]:
    browser_data = load_browser_state(localS)

    st.session_state["project_name"] = browser_data["project_name"]
    st.session_state["proxy_api_key"] = browser_data["proxy_api_key"]
    st.session_state["speed"] = browser_data["speed"]
    st.session_state["pause_ms"] = browser_data["pause_ms"]
    st.session_state["output_name"] = browser_data["output_name"]
    st.session_state["selected_voice_name"] = browser_data["selected_voice_name"]
    st.session_state["main_text"] = browser_data["main_text"]
    st.session_state["input_mode"] = browser_data["input_mode"]
    st.session_state["voices"] = browser_data["voices"]

    st.session_state["browser_bootstrap_done"] = True

st.set_page_config(page_title="LucyLab TTS Studio", layout="wide")
st.title("LucyLab TTS Studio")
st.caption("Streamlit proxy client | lưu cấu hình tạm trên trình duyệt")

voices = st.session_state.get("voices", DEFAULT_VOICES.copy())
if not isinstance(voices, dict) or not voices:
    voices = DEFAULT_VOICES.copy()
    st.session_state["voices"] = voices

engine = TTSEngine()

with st.sidebar:
    st.subheader("Project")
    project_name = st.text_input(
        "Tên project",
        value=st.session_state.get("project_name", "default")
    )
    st.session_state["project_name"] = project_name

    st.subheader("Kết nối")
    api_key = st.text_input(
        "API Key",
        value=st.session_state.get("proxy_api_key", ""),
        type="password"
    )
    st.session_state["proxy_api_key"] = api_key

    st.caption(f"Server cố định: `{PROXY_API_URL}`")

    st.subheader("Cấu hình")
    voice_names = list(voices.keys())
    if not voice_names:
        voices = DEFAULT_VOICES.copy()
        st.session_state["voices"] = voices
        voice_names = list(voices.keys())

    saved_voice = st.session_state.get("selected_voice_name", "Mặc định")
    if saved_voice not in voice_names:
        saved_voice = voice_names[0]

    voice_index = voice_names.index(saved_voice)
    selected_voice_name = st.selectbox("Giọng", voice_names, index=voice_index)
    speed = st.text_input("Speed", value=st.session_state.get("speed", "1.0"))
    pause_ms = st.text_input("Nghỉ giữa câu (ms)", value=st.session_state.get("pause_ms", "300"))
    output_name = st.text_input("Tên file output", value=st.session_state.get("output_name", "final_output.mp3"))

    st.session_state["selected_voice_name"] = selected_voice_name
    st.session_state["speed"] = speed
    st.session_state["pause_ms"] = pause_ms
    st.session_state["output_name"] = output_name

    st.subheader("Quản lý giọng")
    new_voice_name = st.text_input("Tên hiển thị")
    new_voice_id = st.text_input("Voice ID")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Thêm / Cập nhật giọng", use_container_width=True):
            if not new_voice_name.strip() or not new_voice_id.strip():
                st.warning("Nhập đầy đủ Tên hiển thị và Voice ID.")
            else:
                voices[new_voice_name.strip()] = new_voice_id.strip()
                st.session_state["voices"] = voices
                save_browser_state(localS)
                st.success("Đã lưu giọng trên trình duyệt.")
                st.rerun()

    with c2:
        if st.button("Xóa giọng đang chọn", use_container_width=True):
            if selected_voice_name in voices:
                del voices[selected_voice_name]
                if not voices:
                    voices = DEFAULT_VOICES.copy()
                st.session_state["voices"] = voices

                if st.session_state.get("selected_voice_name") not in voices:
                    st.session_state["selected_voice_name"] = list(voices.keys())[0]

                save_browser_state(localS)
                st.success("Đã xóa giọng.")
                st.rerun()

    if st.button("Lưu cấu hình", use_container_width=True):
        save_browser_state(localS)
        st.success("Đã lưu cấu hình trên trình duyệt.")

    if st.button("Xóa dữ liệu trình duyệt", use_container_width=True):
        clear_browser_state(localS)

        st.session_state["project_name"] = DEFAULT_SETTINGS["project_name"]
        st.session_state["proxy_api_key"] = DEFAULT_SETTINGS["proxy_api_key"]
        st.session_state["speed"] = DEFAULT_SETTINGS["speed"]
        st.session_state["pause_ms"] = DEFAULT_SETTINGS["pause_ms"]
        st.session_state["output_name"] = DEFAULT_SETTINGS["output_name"]
        st.session_state["selected_voice_name"] = DEFAULT_SETTINGS["selected_voice_name"]
        st.session_state["main_text"] = DEFAULT_SETTINGS["main_text"]
        st.session_state["input_mode"] = DEFAULT_SETTINGS["input_mode"]
        st.session_state["voices"] = DEFAULT_VOICES.copy()
        st.session_state["runtime_logs"] = []

        st.success("Đã xóa dữ liệu lưu trên trình duyệt.")
        st.rerun()

save_browser_state(localS)

tab1, tab2 = st.tabs(["Tạo audio", "Câu / Preview"])

with tab1:
    left, right = st.columns([2, 1])

    with left:
        st.subheader("Nhập văn bản / SRT")

        uploaded_file = st.file_uploader(
            "Upload .txt hoặc .srt",
            type=["txt", "srt"],
            label_visibility="collapsed"
        )

        loaded_text = ""
        if uploaded_file is not None:
            try:
                raw = uploaded_file.read()
                try:
                    text_data = raw.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        text_data = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        text_data = raw.decode("cp1258")

                if uploaded_file.name.lower().endswith(".srt"):
                    st.session_state["input_mode"] = "srt"
                    loaded_text = text_data
                else:
                    st.session_state["input_mode"] = "text"
                    loaded_text = text_data

                st.session_state["main_text"] = loaded_text
                save_browser_state(localS)

            except Exception as e:
                st.error(f"Lỗi đọc file: {e}")

        default_text = loaded_text if loaded_text else st.session_state.get("main_text", "")
        main_text = st.text_area("Nội dung", value=default_text, height=360, label_visibility="collapsed")
        st.session_state["main_text"] = main_text
        save_browser_state(localS)

        col_btn1, col_btn2, col_btn3, col_btn4 = st.columns([1, 1, 1, 1])
        with col_btn1:
            run_tts_btn = st.button("Chạy TTS", use_container_width=True)
        with col_btn2:
            remerge_btn = st.button("Ghép lại file tổng", use_container_width=True)
        with col_btn3:
            clear_btn = st.button("Xóa nội dung", use_container_width=True)
        with col_btn4:
            clear_runtime_btn = st.button("Xóa file tạm server", use_container_width=True)

        if clear_btn:
            st.session_state["main_text"] = ""
            st.session_state["input_mode"] = "text"
            save_browser_state(localS)
            st.rerun()

        if clear_runtime_btn:
            clear_runtime_files()
            st.success("Đã xóa file tạm trên máy chạy Streamlit.")
            st.rerun()

    with right:
        st.subheader("Trạng thái")
        st.info(f"Project: {st.session_state.get('project_name', 'default')}")
        st.info(f"Input mode: {st.session_state.get('input_mode', 'text')}")

        if api_key.strip():
            key_preview = api_key[:4] + "..." + api_key[-2:] if len(api_key) >= 8 else "***"
            st.success(f"API key: {key_preview}")
        else:
            st.warning("Chưa nhập API key")

        st.subheader("Log")
        logs = st.session_state.get("runtime_logs", [])
        st.code("\n".join(logs[-30:]) if logs else "Chưa có log.")

    if run_tts_btn:
        if not main_text.strip():
            st.warning("Nhập văn bản hoặc upload file trước.")
        elif not api_key.strip():
            st.warning("Nhập API key trước.")
        else:
            try:
                voice_id = st.session_state["voices"][selected_voice_name]
                speed_val = float(speed.strip())
                pause_val = int(pause_ms.strip())

                st.session_state["runtime_logs"] = [
                    "=" * 60,
                    f"Project: {st.session_state.get('project_name', 'default')}",
                    f"Proxy: {PROXY_API_URL}",
                    f"Giọng: {selected_voice_name} | voice_id={voice_id}",
                    f"Speed: {speed_val} | Pause: {pause_val} ms | Mode: {st.session_state['input_mode']}",
                ]

                save_browser_state(localS)

                if st.session_state["input_mode"] == "srt":
                    final_path, logs = asyncio.run(
                        engine.run_tts_srt(
                            srt_text=main_text,
                            api_key=api_key,
                            voice_id=voice_id,
                            speed=speed_val,
                            output_name=output_name,
                            block_version=BLOCK_VERSION,
                        )
                    )
                else:
                    final_path, logs = asyncio.run(
                        engine.run_tts(
                            text=main_text,
                            api_key=api_key,
                            voice_id=voice_id,
                            speed=speed_val,
                            pause_ms=pause_val,
                            output_name=output_name,
                            block_version=BLOCK_VERSION,
                        )
                    )

                st.session_state["runtime_logs"].extend(logs)
                st.success(f"Đã tạo file: {final_path}")

            except Exception as e:
                msg = str(e)
                if "api key" in msg.lower() or "không hợp lệ" in msg.lower():
                    st.error(f"Sai API key: {msg}")
                else:
                    st.error(msg)

    if remerge_btn:
        try:
            pause_val = int(pause_ms.strip())
            final_path = engine.merge_sentences_to_final(
                pause_ms=pause_val,
                output_name=output_name,
            )
            st.success(f"Đã ghép lại file: {final_path}")
        except Exception as e:
            st.error(str(e))

    final_file = OUTPUT_DIR / (output_name if output_name.lower().endswith(".mp3") else output_name + ".mp3")
    if final_file.exists():
        st.subheader("File tổng")
        st.audio(final_file.read_bytes())
        st.download_button(
            "Tải file tổng",
            data=final_file.read_bytes(),
            file_name=final_file.name,
            mime="audio/mpeg",
            use_container_width=True,
        )

with tab2:
    st.subheader("Danh sách câu")
    meta = load_sentences_meta()

    if not meta:
        st.info("Chưa có dữ liệu câu.")
    else:
        sentence_options = [f"{item['index']:03d} - {item.get('timeline', '')} - {item['text'][:50]}" for item in meta]
        selected_option = st.selectbox("Chọn câu", sentence_options)
        selected_idx = int(selected_option.split(" - ")[0])

        item = next((x for x in meta if x["index"] == selected_idx), None)

        if item:
            audio_path = find_sentence_audio(selected_idx)

            c1, c2 = st.columns([2, 1])
            with c1:
                edited_text = st.text_area("Nội dung câu", value=item["text"], height=180)
            with c2:
                st.write(f"**Câu số:** {selected_idx}")
                st.write(f"**Timeline:** {item.get('timeline', '-')}")
                st.write(f"**Trạng thái:** {'Có audio' if audio_path else 'Chưa có audio'}")

            b1, b2, b3 = st.columns([1, 1, 1])

            with b1:
                if st.button("Lưu nội dung câu", use_container_width=True):
                    if not edited_text.strip():
                        st.warning("Nội dung câu không được trống.")
                    else:
                        item["text"] = edited_text.strip()
                        save_sentences_meta(meta)

                        prefix = sentence_file_prefix(selected_idx)
                        txt_path = SENTENCES_DIR / f"{prefix}.txt"
                        txt_path.write_text(edited_text.strip(), encoding="utf-8")

                        old_audio = find_sentence_audio(selected_idx)
                        if old_audio and old_audio.exists():
                            try:
                                old_audio.unlink()
                            except Exception:
                                pass

                        st.success("Đã lưu nội dung câu.")
                        st.rerun()

            with b2:
                if st.button("Tạo lại câu", use_container_width=True):
                    if not api_key.strip():
                        st.warning("Nhập API key trước.")
                    elif not edited_text.strip():
                        st.warning("Nội dung câu không được trống.")
                    else:
                        try:
                            voice_id = st.session_state["voices"][selected_voice_name]
                            speed_val = float(speed.strip())

                            item["text"] = edited_text.strip()
                            save_sentences_meta(meta)

                            audio_path = asyncio.run(
                                engine.regenerate_one_sentence(
                                    idx=selected_idx,
                                    sentence=edited_text.strip(),
                                    api_key=api_key,
                                    voice_id=voice_id,
                                    speed=speed_val,
                                    block_version=BLOCK_VERSION,
                                )
                            )
                            st.success(f"Đã tạo lại câu: {audio_path.name}")
                            st.rerun()
                        except Exception as e:
                            msg = str(e)
                            if "api key" in msg.lower() or "không hợp lệ" in msg.lower():
                                st.error(f"Sai API key: {msg}")
                            else:
                                st.error(msg)

            with b3:
                if st.button("Đồng bộ text chính", use_container_width=True):
                    meta_now = load_sentences_meta()
                    if any("timeline" in x for x in meta_now):
                        blocks = []
                        for it in sorted(meta_now, key=lambda x: x["index"]):
                            blocks.append(str(it["index"]))
                            blocks.append(it.get("timeline", ""))
                            blocks.append(it["text"])
                            blocks.append("")
                        st.session_state["main_text"] = "\n".join(blocks).strip()
                        st.session_state["input_mode"] = "srt"
                    else:
                        st.session_state["main_text"] = rebuild_full_text_from_meta(meta_now)
                        st.session_state["input_mode"] = "text"

                    save_browser_state(localS)
                    st.success("Đã đồng bộ.")
                    st.rerun()

            if audio_path and audio_path.exists():
                st.subheader("Preview audio")
                st.audio(audio_path.read_bytes())
                st.download_button(
                    "Tải audio câu này",
                    data=audio_path.read_bytes(),
                    file_name=audio_path.name,
                    mime="audio/mpeg",
                    use_container_width=False,
                )
