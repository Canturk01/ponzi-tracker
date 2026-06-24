"""
etherscan_analyzer.py
Telegram mesajlarından çıkarılan cüzdan adreslerini Etherscan API ile analiz eder.
"""

import re
import httpx
from typing import Optional
from dataclasses import dataclass, field

ETHERSCAN_BASE = "https://api.etherscan.io/api"

# ETH / ERC-20 adres regex
WALLET_PATTERN = re.compile(r'\b(0x[a-fA-F0-9]{40})\b')
# TRON adres regex
TRON_PATTERN = re.compile(r'\b(T[a-zA-Z0-9]{33})\b')


@dataclass
class WalletRisk:
    address: str
    chain: str  # ETH / TRON
    balance_eth: float = 0.0
    tx_count: int = 0
    age_days: int = 0
    fan_in_count: int = 0       # Kaç farklı adresten para aldı
    fan_out_count: int = 0      # Kaç farklı adrese para gönderdi
    mixer_interaction: bool = False
    risk_score: int = 0
    risk_flags: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "address": self.address,
            "chain": self.chain,
            "balance_eth": self.balance_eth,
            "tx_count": self.tx_count,
            "age_days": self.age_days,
            "fan_in_count": self.fan_in_count,
            "fan_out_count": self.fan_out_count,
            "mixer_interaction": self.mixer_interaction,
            "risk_score": self.risk_score,
            "risk_flags": self.risk_flags,
        }


# Bilinen mixer/tumbler adresleri (kısmi liste — üretimde genişletilmeli)
KNOWN_MIXERS = {
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",  # Tornado Cash Router
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",  # Tornado Cash
    "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",  # Tornado Cash
    "0xa160cdab225685da1d56aa342ad8841c3b53f291",  # Tornado Cash
}


def extract_wallets(text: str) -> list[str]:
    """Mesaj metninden ETH cüzdan adreslerini çıkarır."""
    eth_addrs = WALLET_PATTERN.findall(text)
    return list(set(addr.lower() for addr in eth_addrs))


async def analyze_wallet(address: str, api_key: str) -> WalletRisk:
    """Etherscan API ile cüzdan analizi yapar."""
    wallet = WalletRisk(address=address, chain="ETH")

    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1. Bakiye
        try:
            r = await client.get(ETHERSCAN_BASE, params={
                "module": "account",
                "action": "balance",
                "address": address,
                "tag": "latest",
                "apikey": api_key,
            })
            data = r.json()
            if data.get("status") == "1":
                wallet.balance_eth = int(data["result"]) / 1e18
        except Exception:
            pass

        # 2. Normal işlem geçmişi
        try:
            r = await client.get(ETHERSCAN_BASE, params={
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "sort": "asc",
                "apikey": api_key,
            })
            data = r.json()
            if data.get("status") == "1":
                txs = data["result"]
                wallet.tx_count = len(txs)

                if txs:
                    import time
                    first_ts = int(txs[0]["timeStamp"])
                    wallet.age_days = max(1, int((time.time() - first_ts) / 86400))

                    # Fan-in: bu adrese para gönderen benzersiz adres sayısı
                    senders = set(
                        tx["from"].lower() for tx in txs
                        if tx["to"].lower() == address.lower()
                    )
                    wallet.fan_in_count = len(senders)

                    # Fan-out: bu adresin para gönderdiği benzersiz adresler
                    receivers = set(
                        tx["to"].lower() for tx in txs
                        if tx["from"].lower() == address.lower()
                    )
                    wallet.fan_out_count = len(receivers)

                    # Mixer etkileşimi
                    all_parties = senders | receivers
                    if all_parties & KNOWN_MIXERS:
                        wallet.mixer_interaction = True
        except Exception:
            pass

    # Risk skoru hesapla
    _calculate_onchain_risk(wallet)
    return wallet


def _calculate_onchain_risk(wallet: WalletRisk):
    """On-chain verilerden risk skoru üretir."""
    score = 0
    flags = []

    # Yeni cüzdan (< 30 gün)
    if wallet.age_days < 30:
        score += 25
        flags.append(f"Yeni cüzdan ({wallet.age_days} gün)")

    # Çok sayıda gönderen (fan-in — Ponzi toplama imzası)
    if wallet.fan_in_count > 50:
        score += 30
        flags.append(f"Fan-in: {wallet.fan_in_count} farklı gönderen")
    elif wallet.fan_in_count > 20:
        score += 15
        flags.append(f"Orta fan-in: {wallet.fan_in_count} gönderen")

    # Mixer etkileşimi
    if wallet.mixer_interaction:
        score += 35
        flags.append("Bilinen mixer ile etkileşim (Tornado Cash)")

    # Yüksek işlem hacmi kısa sürede
    if wallet.age_days > 0 and wallet.tx_count / wallet.age_days > 20:
        score += 10
        flags.append(f"Yüksek işlem yoğunluğu: {wallet.tx_count / wallet.age_days:.1f}/gün")

    wallet.risk_score = min(score, 100)
    wallet.risk_flags = flags
