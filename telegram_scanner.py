"""
telegram_scanner.py
Bot Token ile Telegram gruplarını Ponzi kalıpları için tarar.
Oturum dosyası gerektirmez — Render ücretsiz plan ile uyumlu.
"""

import asyncio
import logging
import httpx
from datetime import datetime, timezone
from typing import Optional

from ponzi_scorer import score_group_messages
from etherscan_analyzer import analyze_wallet, extract_wallets

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot"

DEFAULT_TARGETS = [
    "cryptoinvestment", "cryptoprofit", "dailycryptoprofit",
    "cryptoyield", "bitcoininvestors", "defiearnings",
    "cryptopassiveincome", "guaranteedcrypto",
]


class TelegramScanner:
    def __init__(self, bot_token: str, etherscan_key: str, api_id: int = 0, api_hash: str = ""):
        self.bot_token = bot_token
        self.etherscan_key = etherscan_key
        self.results: list[dict] = []

    async def connect(self):
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{TG_API}{self.bot_token}/getMe")
            data = r.json()
            if data.get("ok"):
                logger.info(f"Bot bağlandı: @{data['result'].get('username')}")
            else:
                raise ValueError(f"Bot token geçersiz: {data}")

    async def disconnect(self):
        pass

    async def get_chat_info(self, username: str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                r = await client.get(
                    f"{TG_API}{self.bot_token}/getChat",
                    params={"chat_id": f"@{username}"}
                )
                data = r.json()
                if data.get("ok"):
                    chat = data["result"]
                    return {
                        "id": chat.get("id"),
                        "title": chat.get("title", username),
                        "username": username,
                        "members": chat.get("members_count", 0),
                        "description": chat.get("description", ""),
                    }
            except Exception as e:
                logger.error(f"Grup bilgisi alınamadı (@{username}): {e}")
        return None

    async def scan_group_by_description(self, username: str) -> dict:
        result = {
            "group": username,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "message_count": 0,
            "ponzi_score": None,
            "wallets": [],
            "error": None,
        }

        chat_info = await self.get_chat_info(username)
        if not chat_info:
            result["error"] = "Grup bulunamadı veya erişilemiyor"
            return result

        result["group_meta"] = chat_info
        texts = []
        if chat_info.get("title"):
            texts.append(chat_info["title"])
        if chat_info.get("description"):
            texts.append(chat_info["description"])

        if texts:
            result["message_count"] = len(texts)
            score = score_group_messages(texts)
            result["ponzi_score"] = score.to_dict()

            all_text = " ".join(texts)
            wallet_addresses = extract_wallets(all_text)

            if score.total >= 40 and wallet_addresses:
                wallet_results = []
                for addr in list(wallet_addresses)[:5]:
                    try:
                        w = await analyze_wallet(addr, self.etherscan_key)
                        wallet_results.append(w.to_dict())
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error(f"Cüzdan analiz hatası ({addr}): {e}")
                result["wallets"] = wallet_results

        return result

    async def search_groups(self, keywords: list[str], limit_per_keyword: int = 10) -> list[dict]:
        targets = set(DEFAULT_TARGETS)
        for kw in keywords:
            clean = kw.lower().replace(" ", "").replace("%", "percent")
            targets.add(f"crypto{clean}")
            targets.add(f"{clean}invest")
            targets.add(f"{clean}earn")

        found = []
        for username in list(targets)[:30]:
            info = await self.get_chat_info(username)
            if info:
                found.append(info)
                logger.info(f"Grup bulundu: @{username}")
            await asyncio.sleep(0.5)
        return found

    async def run_scan(self, keywords, risk_threshold=60, message_limit=100, on_result=None):
        self.results = []
        groups = await self.search_groups(keywords)
        logger.info(f"{len(groups)} grup bulundu, taranıyor...")

        for group in groups:
            username = group.get("username")
            if not username:
                continue
            scan = await self.scan_group_by_description(username)
            ponzi_total = scan.get("ponzi_score", {}).get("total", 0) if scan.get("ponzi_score") else 0
            if ponzi_total >= risk_threshold:
                self.results.append(scan)
                logger.info(f"ALERT: @{username} skor={ponzi_total}")
                if on_result:
                    await on_result(scan)
            await asyncio.sleep(1)

        return self.results
