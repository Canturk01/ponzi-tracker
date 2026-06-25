import time
import random
import re
import requests
from bs4 import BeautifulSoup

# CONFIGURATION
BACKEND_API = "https://ponzi-tracker.onrender.com/scan/ingest"

# Taranacak public grup listesi — buraya şüpheli grup adlarını ekle
TARGET_GROUPS = [
    "moonrisecapital",
    "defiharvestpro", 
    "cryptoyield_dao",
    "globalarbfund",
    # Buraya yeni gruplar ekleyebilirsin:
    # "yeni_supheceli_grup",
]

def scrape_telegram_web_preview(group_name):
    """t.me/s/ üzerinden hesap gerekmeden public grup mesajlarını çeker."""
    url = f"https://t.me/s/{group_name}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"  ⚠ HTTP {response.status_code} — grup özel veya bulunamadı")
            return None

        soup = BeautifulSoup(response.text, 'html.parser')

        title_el = soup.find("div", {"class": "tgme_channel_info_header_title"})
        if not title_el:
            title_el = soup.find("span", {"class": "tgme_channel_info_header_title"})
        title = title_el.text.strip() if title_el else group_name

        members = 0
        for counter in soup.find_all("div", {"class": "tgme_channel_info_counter"}):
            text = counter.text.strip().lower()
            if "member" in text or "subscriber" in text or "abone" in text:
                digits = ''.join(filter(str.isdigit, text))
                if digits:
                    members = int(digits)
                    break

        msg_els = soup.find_all("div", {"class": "tgme_widget_message_text"})
        messages = [m.get_text(separator=" ").strip() for m in msg_els if m.get_text().strip()]

        print(f"  ✓ {title} — {members} üye — {len(messages)} mesaj")
        return {
            "group": group_name,
            "title": title,
            "members": members,
            "messages": messages,
        }
    except Exception as e:
        print(f"  ✗ Hata: {e}")
        return None


def analyze_ponzi_signals(scraped_data):
    """Mesajlardan Ponzi sinyali çıkar ve skor hesapla."""
    messages = scraped_data["messages"]
    
    # Mesaj yoksa açıklama/başlığı analiz et
    if not messages:
        messages = [scraped_data.get("title", "")]

    rules = [
        (r"(guaranteed|safe|no risk|garanti|risksiz|kayıp yok)", "Garanti Getiri Vaadi", 35, "high"),
        (r"(daily|günlük|%\s*\d+|profit|kazanç)", "Yüksek Getiri Oranı", 30, "high"),
        (r"(referral|invite|commission|komisyon|davet et|getir kazan)", "Referral Piramit", 25, "medium"),
        (r"(payment proof|ödeme kanıt|withdrawn|çekim)", "Sahte Ödeme Kanıtı", 15, "medium"),
        (r"(join now|hemen katıl|son \d+ (gün|saat)|limited)", "FOMO / Aciliyet", 10, "low"),
    ]

    all_text = " ".join(messages).lower()
    signal_hits = []
    telegram_score = 0

    for pattern, desc, score, severity in rules:
        matches = re.findall(pattern, all_text, re.IGNORECASE)
        if matches:
            signal_hits.append({
                "description": desc,
                "matched_text": f"'{matches[0]}' — {len(matches)} eşleşme",
                "severity": severity,
                "score": score,
            })
            telegram_score += score

    telegram_score = min(telegram_score, 100)
    total = min(int(telegram_score * 0.9 + 10), 100)
    verdict = "Kritik" if total >= 70 else ("Orta Risk" if total >= 40 else "İzlemede")

    # Matematiksel imkânsızlık — mesajdan getiri oranı çıkarmaya çalış
    imp = None
    daily = re.search(r'(\d+(?:[.,]\d+)?)\s*%\s*(daily|günlük)', all_text)
    monthly = re.search(r'(\d+(?:[.,]\d+)?)\s*%\s*(monthly|aylık)', all_text)
    if daily:
        rate = float(daily.group(1).replace(",", "."))
        imp = round((1 + rate / 100) ** 365, 1)
    elif monthly:
        rate = float(monthly.group(1).replace(",", "."))
        imp = round((1 + rate / 100) ** 12, 1)

    return {
        "group": scraped_data["group"],
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message_count": len(messages),
        "group_meta": {
            "title": scraped_data["title"],
            "members": scraped_data["members"],
            "username": scraped_data["group"],
        },
        "ponzi_score": {
            "total": total,
            "telegram_score": telegram_score,
            "verdict": verdict,
            "impossibility_multiplier": imp,
            "signal_hits": signal_hits,
        },
        "wallets": [],
        "error": None,
    }


def push_to_backend(payload):
    """Analiz sonucunu Render backend'ine gönder."""
    try:
        r = requests.post(BACKEND_API, json=payload, timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"  📡 Backend: {data.get('status')} — toplam {data.get('total', '?')} vaka")
        else:
            print(f"  ⚠ Backend yanıt kodu: {r.status_code}")
    except Exception as e:
        print(f"  ✗ Backend bağlantı hatası: {e}")


# Ana döngü
print("=" * 55)
print("  PonziTracker Web Scraper")
print("  t.me/s/ üzerinden hesapsız tarama")
print("=" * 55)

for group in TARGET_GROUPS:
    print(f"\n▶ Taranıyor: @{group}")
    data = scrape_telegram_web_preview(group)

    if not data:
        data = {
            "group": group,
            "title": group.upper(),
            "members": 0,
            "messages": [],
        }

    analysis = analyze_ponzi_signals(data)
    score = analysis["ponzi_score"]["total"]
    verdict = analysis["ponzi_score"]["verdict"]
    print(f"  Skor: {score}/100 — {verdict}")
    push_to_backend(analysis)
    time.sleep(2)

print("\n✓ Tarama tamamlandı — dashboard'ı yenile")
