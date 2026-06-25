"""
main.py
FastAPI backend — Dashboard ile Telegram/Etherscan arasındaki köprü.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Ortam değişkenleri
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
RISK_THRESHOLD = int(os.getenv("RISK_THRESHOLD", "60"))
PONZI_KEYWORDS = os.getenv(
    "PONZI_KEYWORDS",
    "garanti getiri,garantili kazanç,günlük %,günde %,aylık %,referral"
).split(",")

# Global state
scan_results: list[dict] = []
scan_status = {"running": False, "started_at": None, "groups_found": 0, "groups_scanned": 0}
scanner_instance = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("PonziTracker API başlatıldı")
    yield
    if scanner_instance:
        await scanner_instance.disconnect()


app = FastAPI(title="PonziTracker API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response modelleri
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    keywords: Optional[list[str]] = None
    risk_threshold: Optional[int] = None
    message_limit: int = 100


class WalletRequest(BaseModel):
    address: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN),
        "etherscan_configured": bool(ETHERSCAN_API_KEY),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/scan/start")
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks):
    """Arka planda Telegram taraması başlatır."""
    if scan_status["running"]:
        raise HTTPException(status_code=409, detail="Tarama zaten devam ediyor")

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise HTTPException(status_code=400, detail=".env dosyasında Telegram credentials eksik")

    keywords = req.keywords or PONZI_KEYWORDS
    threshold = req.risk_threshold or RISK_THRESHOLD

    background_tasks.add_task(_run_scan_task, keywords, threshold, req.message_limit)

    return {
        "status": "started",
        "keywords": keywords,
        "risk_threshold": threshold,
        "message": "Tarama arka planda çalışıyor. /scan/status ile takip edin.",
    }


@app.get("/scan/status")
async def get_scan_status():
    return {**scan_status, "results_count": len(scan_results)}


@app.get("/scan/results")
async def get_results(min_score: int = 0, limit: int = 50):
    """Tarama sonuçlarını döner — isteğe bağlı minimum skor filtresi."""
    filtered = [
        r for r in scan_results
        if r.get("ponzi_score", {}).get("total", 0) >= min_score
    ]
    filtered.sort(key=lambda x: x.get("ponzi_score", {}).get("total", 0), reverse=True)
    return {"results": filtered[:limit], "total": len(filtered)}


@app.get("/scan/results/{group_username}")
async def get_group_result(group_username: str):
    """Belirli bir grubun detaylı sonucunu döner."""
    for r in scan_results:
        if r.get("group") == group_username:
            return r
    raise HTTPException(status_code=404, detail="Grup bulunamadı")


@app.post("/wallet/analyze")
async def analyze_single_wallet(req: WalletRequest):
    """Tek bir ETH cüzdanını on-chain analiz eder."""
    if not ETHERSCAN_API_KEY:
        raise HTTPException(status_code=400, detail="Etherscan API key eksik")

    from etherscan_analyzer import analyze_wallet
    try:
        result = await analyze_wallet(req.address.lower(), ETHERSCAN_API_KEY)
        return result.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/scan/clear")
async def clear_results():
    """Sonuçları temizler."""
    scan_results.clear()
    return {"status": "cleared"}


# ---------------------------------------------------------------------------
# Arka plan tarama görevi
# ---------------------------------------------------------------------------

async def _run_scan_task(keywords: list[str], threshold: int, message_limit: int):
    global scanner_instance

    scan_status["running"] = True
    scan_status["started_at"] = datetime.now(timezone.utc).isoformat()
    scan_status["groups_found"] = 0
    scan_status["groups_scanned"] = 0
    scan_results.clear()

    try:
        from telegram_scanner import TelegramScanner

        scanner_instance = TelegramScanner(
            bot_token=TELEGRAM_BOT_TOKEN,
            etherscan_key=ETHERSCAN_API_KEY,
            api_id=TELEGRAM_API_ID,
            api_hash=TELEGRAM_API_HASH,
        )
        await scanner_instance.connect()

        # Grup ara
        groups = await scanner_instance.search_groups(keywords)
        scan_status["groups_found"] = len(groups)
        logger.info(f"{len(groups)} grup bulundu")

        # Her grubu tara
        for i, group in enumerate(groups):
            username = group.get("username")
            if not username:
                continue

            scan = await scanner_instance.scan_group(username, message_limit)
            scan["group_meta"] = group
            scan_status["groups_scanned"] = i + 1

            ponzi_total = scan.get("ponzi_score", {}).get("total", 0) if scan.get("ponzi_score") else 0
            if ponzi_total >= threshold:
                scan_results.append(scan)
                logger.info(f"ALERT: @{username} skor={ponzi_total}")

            await asyncio.sleep(1)

        await scanner_instance.disconnect()

    except Exception as e:
        logger.error(f"Tarama hatası: {e}")
    finally:
        scan_status["running"] = False
        logger.info(f"Tarama tamamlandı. {len(scan_results)} alert.")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
