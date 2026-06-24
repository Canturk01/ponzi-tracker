"""
telegram_scanner.py
Telethon ile public Telegram gruplarını Ponzi kalıpları için tarar.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel
from telethon.errors import FloodWaitError, ChatAdminRequiredError

from ponzi_scorer import score_group_messages, extract_wallets_from_messages
from etherscan_analyzer import analyze_wallet, extract_wallets

logger = logging.getLogger(__name__)


class TelegramScanner:
    def __init__(self, api_id: int, api_hash: str, etherscan_key: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.etherscan_key = etherscan_key
        self.client: Optional[TelegramClient] = None
        self.results: list[dict] = []

    async def connect(self):
        """Telegram'a bağlan — ilk çalıştırmada telefon kodu sorar."""
        self.client = TelegramClient(
            "ponzitracker_session",
            self.api_id,
            self.api_hash,
        )
        await self.client.start()
        logger.info("Telegram bağlantısı kuruldu.")

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()

    async def search_groups(self, keywords: list[str], limit_per_keyword: int = 10) -> list[dict]:
        """
        Anahtar kelimelerle public grup araması yapar.
        Sonuç: grup metadata listesi.
        """
        found_groups: dict[int, dict] = {}

        for keyword in keywords:
            logger.info(f"Arama: '{keyword}'")
            try:
                result = await self.client(SearchRequest(
                    q=keyword,
                    limit=limit_per_keyword,
                ))
                for chat in result.chats:
                    if isinstance(chat, Channel) and chat.id not in found_groups:
                        found_groups[chat.id] = {
                            "id": chat.id,
                            "title": chat.title,
                            "username": getattr(chat, "username", None),
                            "members": getattr(chat, "participants_count", 0),
                            "found_via": keyword,
                        }
                # Flood koruması — Telegram rate limit
                await asyncio.sleep(2)

            except FloodWaitError as e:
                logger.warning(f"Rate limit: {e.seconds}s bekleniyor")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Arama hatası ({keyword}): {e}")

        return list(found_groups.values())

    async def scan_group(self, group_username: str, message_limit: int = 100) -> dict:
        """
        Tek bir grubun son mesajlarını okur, Ponzi skoru hesaplar,
        cüzdan adreslerini on-chain analize gönderir.
        """
        result = {
            "group": group_username,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "message_count": 0,
            "ponzi_score": None,
            "wallets": [],
            "error": None,
        }

        try:
            entity = await self.client.get_entity(group_username)
            messages = []
            wallet_addresses = set()

            async for msg in self.client.iter_messages(entity, limit=message_limit):
                if msg.text:
                    messages.append(msg.text)
                    # Mesajdan cüzdan adresi çıkar
                    for addr in extract_wallets(msg.text):
                        wallet_addresses.add(addr)

                # Flood koruması
                await asyncio.sleep(0.05)

            result["message_count"] = len(messages)

            # Ponzi skoru
            if messages:
                score = score_group_messages(messages)
                result["ponzi_score"] = score.to_dict()

            # On-chain analiz — yalnızca yüksek riskli gruplarda
            ponzi_total = result["ponzi_score"]["total"] if result["ponzi_score"] else 0
            if ponzi_total >= 40 and wallet_addresses:
                logger.info(f"{group_username}: {len(wallet_addresses)} cüzdan analiz ediliyor")
                wallet_results = []
                for addr in list(wallet_addresses)[:10]:  # Max 10 adres
                    try:
                        w = await analyze_wallet(addr, self.etherscan_key)
                        wallet_results.append(w.to_dict())
                        await asyncio.sleep(0.3)  # Etherscan rate limit
                    except Exception as e:
                        logger.error(f"Cüzdan analiz hatası ({addr}): {e}")
                result["wallets"] = wallet_results

        except ChatAdminRequiredError:
            result["error"] = "Grup özel — erişim yok"
        except FloodWaitError as e:
            result["error"] = f"Rate limit: {e.seconds}s"
            await asyncio.sleep(e.seconds)
        except Exception as e:
            result["error"] = str(e)

        return result

    async def run_scan(
        self,
        keywords: list[str],
        risk_threshold: int = 60,
        message_limit: int = 100,
        on_result=None,
    ) -> list[dict]:
        """
        Tam tarama döngüsü:
        1. Anahtar kelimelerle grup bul
        2. Her grubu tara
        3. Eşiği geçenleri raporla
        """
        self.results = []
        groups = await self.search_groups(keywords)
        logger.info(f"{len(groups)} grup bulundu, taranıyor...")

        for group in groups:
            username = group.get("username")
            if not username:
                continue

            scan = await self.scan_group(username, message_limit)
            scan["group_meta"] = group

            ponzi_total = 0
            if scan.get("ponzi_score"):
                ponzi_total = scan["ponzi_score"].get("total", 0)

            if ponzi_total >= risk_threshold:
                self.results.append(scan)
                logger.info(
                    f"ALERT: @{username} — skor {ponzi_total} "
                    f"(eşik: {risk_threshold})"
                )
                if on_result:
                    await on_result(scan)

            await asyncio.sleep(1)

        return self.results
