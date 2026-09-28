"""
Сервер верификации для сайта chisto-tochka-spb.ru
Использует звонки-авторизацию (Code Call) через sms.ru
"""

import time
import random
import re
import ipaddress
from collections import defaultdict
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

codes = {}
ip_requests = defaultdict(list)
phone_requests = defaultdict(list)

MAX_CALLS_PER_IP = 3
MAX_CALLS_PER_PHONE = 1
CODE_EXPIRE = 300


class SendCodeRequest(BaseModel):
    phone: str
    captcha: str
    captcha_answer: str


class VerifyCodeRequest(BaseModel):
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
    """Проверяем, является ли IP локальным/внутренним"""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except:
        return True


def get_client_ip(request: Request) -> str:
    """Получаем реальный IP клиента, или -1 если не можем определить"""
    # Проверяем заголовки прокси
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        if ip and not is_private_ip(ip):
            return ip

    # Проверяем direct IP
    if request.client and request.client.host:
        ip = request.client.host
        if not is_private_ip(ip):
            return ip

    # Если IP локальный или не определён — передаём -1
    # sms.ru требует именно -1 для локальных/серверных IP
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


@app.post("/send-code")
async def send_code(req: SendCodeRequest, request: Request):
    """Отправка звонка-авторизации через sms.ru"""
    # Проверка капчи
    if not req.captcha or not req.captcha_answer:
        return {"success": False, "error": "Пройдите проверку"}

    try:
        user_answer = int(req.captcha)
        correct_answer = int(req.captcha_answer)
        if user_answer != correct_answer:
            return {"success": False, "error": "Неверный ответ на проверку"}
    except (ValueError, TypeError):
        return {"success": False, "error": "Неверный ответ на проверку"}

    # Нормализуем телефон
    phone = normalize_phone(req.phone)
    if not is_valid_phone(phone):
        return {"success": False, "error": "Введите корректный номер телефона"}

    # Получаем IP клиента
    client_ip = get_client_ip(request)

    # Проверяем лимиты
    if not check_ip_limit(client_ip):
        return {"success": False, "error": "Слишком много запросов. Попробуйте позже."}
    if not check_phone_limit(phone):
        return {"success": False, "error": "Звонок уже отправлен. Проверьте входящий вызов."}

    import os
    api_id = os.environ.get("SMSRU_API_ID", "")
    if not api_id:
        return {"success": False, "error": "Сервер не настроен"}

    # Отправляем звонок через sms.ru Code Call
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://sms.ru/code/call",
            params={
                "api_id": api_id,
                "phone": phone,
                "ip": client_ip,
                "json": 1,
            },
        )
        data = resp.json()

    print(f"[SMS-Verify] sms.ru response: {data}")

    if data.get("status") == "OK":
        call_code = str(data.get("code", ""))
        if not call_code:
            return {"success": False, "error": "Ошибка при отправке звонка"}

        codes[phone] = {"code": call_code, "expires": time.time() + CODE_EXPIRE}
        return {"success": True, "message": "Звонок отправлен на номер " + phone}
    else:
        error_msg = data.get("status_text", "")
        if not error_msg:
            error_msg = "Не удалось позвонить. Проверьте номер."
        print(f"[SMS-Verify] Error from sms.ru: {error_msg}")
        return {"success": False, "error": error_msg}


@app.post("/verify-code")
async def verify_code(req: VerifyCodeRequest):
    phone = normalize_phone(req.phone)
    if not phone:
        return {"success": False, "error": "Неверный номер"}

    stored = codes.get(phone)
    if not stored:
        return {"success": False, "error": "Запросите звонок повторно"}

    if time.time() > stored["expires"]:
        del codes[phone]
        return {"success": False, "error": "Код истёк. Запросите новый звонок."}

    if req.code.strip() == stored["code"]:
        del codes[phone]
        return {"success": True, "message": "Номер подтверждён"}
    else:
        return {"success": False, "error": "Неверный код"}


@app.get("/")
async def health():
    return {"status": "ok"}
