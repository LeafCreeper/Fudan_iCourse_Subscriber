import os

STUDENT_ID = os.environ.get("StuId", "")
PASSWORD = os.environ.get("UISPsw", "")

WEBVPN_BASE = "https://webvpn.fudan.edu.cn"
IDP_BASE = "https://id.fudan.edu.cn"
ICOURSE_BASE = "https://icourse.fudan.edu.cn"

WEBVPN_AES_KEY = b"wrdvpnisthebest!"
WEBVPN_AES_IV = b"wrdvpnisthebest!"

TENANT_CODE = "222"
GROUP_CODE = "2095000001"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# LLM (ModelScope OpenAI-compatible API)
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
LLM_BASE_URL = "https://api-inference.modelscope.cn/v1/"
LLM_MODELS = [
    "ZhipuAI/GLM-5",
    "MiniMax/MiniMax-M2.5",
    "deepseek-ai/DeepSeek-V3.2",
    "ZhipuAI/GLM-4.7",
    "deepseek-ai/DeepSeek-V3-0324",
    "Qwen/Qwen3.5-397B-A17B",
    "Qwen/Qwen2.5-72B-Instruct"
]

# QQ SMTP
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "")
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

# Database & Storage
DATA_DIR = os.environ.get("DATA_DIR", "data")
VIDEO_DIR = os.path.join(DATA_DIR, "videos")
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "icourse.db"))

# SenseVoice STT (sherpa-onnx)
SENSEVOICE_MODEL_DIR = os.environ.get(
    "SENSEVOICE_MODEL_DIR",
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
)
SILERO_VAD_PATH = os.environ.get("SILERO_VAD_PATH", "silero_vad.onnx")

# 监控的课程 ID 列表
COURSE_IDS = [
    c.strip()
    for c in os.environ.get("COURSE_IDS", "").split(",")
    if c.strip()
]
