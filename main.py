"""
Сервер Flash Call верификации для сайта chisto-tochka-spb.ru
Авторизация звонком через Plusofon Flash Call API
"""

import time
import re
import ipaddress
from collections import defaultdict
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Хранилище ключей вызова: phone -> {key, expires}
call_keys = {}
ip_requests = defaultdict(list)
phone_requests = defaultdict(list)

MAX_CALLS_PER_IP = 5
MAX_CALLS_PER_PHONE = 3
KEY_EXPIRE = 600  # 10 минут

PLUSOFON_CLIENT_ID = os.environ.get("PLUSOFON_CLIENT_ID", "2224")
PLUSOFON_ACCESS_TOKEN = os.environ.get("PLUSOFON_ACCESS_TOKEN", "7TdgmHfYczspIaFdtoXh6mZxdBUIwKpX")
PLUSOFON_API_URL = "https://restapi.plusofon.ru/api/v1/flash-call"


class SendCallRequest(BaseModel):
    phone: str
    captcha: str
    captcha_answer: str


class VerifyCallRequest(BaseModel):
    phone: str
    code: str


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return digits
    if len(digits) == 10:
        return "7" + digits
    return ""


def is_valid_phone(phone: str) -> bool:
    if not phone or len(phone) != 11:
        return False
    if not phone.startswith("7"):
        return False
    if phone[1] != "9":
        return False
    return True


def is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except:
        return True


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        if ip and not is_private_ip(ip):
            return ip
    if request.client and request.client.host:
        ip = request.client.host
        if not is_private_ip(ip):
            return ip
    return "-1"


def check_ip_limit(ip: str) -> bool:
    now = time.time()
    ip_requests[ip] = [t for t in ip_requests[ip] if now - t < 3600]
    if len(ip_requests[ip]) >= MAX_CALLS_PER_IP:
        return False
    ip_requests[ip].append(now)
    return True


def check_phone_limit(phone: str) -> bool:
    now = time.time()
    phone_requests[phone] = [t for t in phone_requests[phone] if now - t < 3600]
    if len(phone_requests[phone]) >= MAX_CALLS_PER_PHONE:
        return False
    phone_requests[phone].append(now)
    return True


def get_plusofon_headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Client": PLUSOFON_CLIENT_ID,
        "Authorization": f"Bearer {PLUSOFON_ACCESS_TOKEN}",
    }


@app.post("/send-code")
async def send_code(req: SendCallRequest, request: Request):
    """Отправка Flash Call через Plusofon API"""
    if not req.captcha or not req.captcha_answer:
        return {"success": False, "error": "Пройдите проверку"}

    try:
        user_answer = int(req.captcha)
        correct_answer = int(req.captcha_answer)
        if user_answer != correct_answer:
            return {"success": False, "error": "Неверный ответ на проверку"}
    except (ValueError, TypeError):
        return {"success": False, "error": "Неверный ответ на проверку"}

    phone = normalize_phone(req.phone)
    if not is_valid_phone(phone):
        return {"success": False, "error": "Введите корректный номер телефона"}

    client_ip = get_client_ip(request)

    if not check_ip_limit(client_ip):
        return {"success": False, "error": "Слишком много запросов. Попробуйте позже."}
    if not check_phone_limit(phone):
        return {"success": False, "error": "Звонок уже отправлен. Проверьте телефон."}

    headers = get_plusofon_headers()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PLUSOFON_API_URL}/send",
            headers=headers,
            json={"phone": phone},
        )
        data = resp.json()

    print(f"[FlashCall] Plusofon send response: {data}")

    # Plusofon возвращает data.key при успехе
    resp_data = data.get("data", {})
    if isinstance(resp_data, dict) and "key" in resp_data:
        key = resp_data["key"]
        call_keys[phone] = {"key": key, "expires": time.time() + KEY_EXPIRE}
        return {"success": True, "message": "Звонок отправлен на номер " + phone}
    else:
        error_msg = data.get("message", data.get("error", "Не удалось отправить звонок. Проверьте номер."))
        print(f"[FlashCall] Error from Plusofon: {error_msg}")
        return {"success": False, "error": error_msg}


@app.post("/verify-code")
async def verify_code(req: VerifyCallRequest):
    phone = normalize_phone(req.phone)
    if not phone:
        return {"success": False, "error": "Неверный номер"}

    stored = call_keys.get(phone)
    if not stored:
        return {"success": False, "error": "Запросите звонок повторно"}

    if time.time() > stored["expires"]:
        del call_keys[phone]
        return {"success": False, "error": "Время истекло. Запросите новый звонок."}

    headers = get_plusofon_headers()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PLUSOFON_API_URL}/check",
            headers=headers,
            json={"pin": req.code.strip(), "key": stored["key"]},
        )
        data = resp.json()

    print(f"[FlashCall] Plusofon check response: {data}")

    # Проверяем разные форматы ответа Плюсофона
    resp_data = data.get("data", {})
    if (
        data.get("status") == "OK"
        or (isinstance(resp_data, dict) and resp_data.get("verified") is True)
        or data.get("result") is True
        or (isinstance(resp_data, dict) and resp_data.get("status") == "verified")
    ):
        del call_keys[phone]
        return {"success": True, "message": "Номер подтверждён"}
    else:
        return {"success": False, "error": "Неверный код"}


@app.get("/")
async def health():
    return {"status": "ok"}
