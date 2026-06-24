"""
ponzi_scorer.py
Mesaj ve grup verilerinden Ponzi risk skoru hesaplar.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Sinyal tanımları — her sinyal: (regex_pattern, puan, açıklama)
# ---------------------------------------------------------------------------

TELEGRAM_SIGNALS = [
    # Garanti getiri vaatleri — en yüksek ağırlık
    (r'(garanti(li)?\s*(getiri|kazanç|kâr|kar|gelir))', 25, "Garanti getiri vaadi"),
    (r'(günde?\s*%\s*\d+)', 25, "Günlük yüzde vaadi"),
    (r'(aylık\s*%\s*\d{3,})', 20, "Yüksek aylık yüzde vaadi"),
    (r'(günlük\s*(kazanç|getiri|kâr))', 20, "Günlük kazanç vaadi"),
    (r'(risk\s*yok|risksiz|kayıp\s*yok|sıfır\s*risk)', 20, "Risksiz yatırım iddiası"),

    # Referral / piramit yapısı
    (r'(referral|referans|davet\s*et\s*kazan)', 18, "Referral sistemi"),
    (r'(kişi\s*getir|arkadaş\s*getir|üye\s*getir)', 18, "Piramit üye kazanımı"),
    (r'(pasif\s*gelir|pasif\s*kazanç)', 15, "Pasif gelir vaadi"),
    (r'(komisyon\s*%\s*\d+)', 15, "Yüksek komisyon vaadi"),

    # Aciliyet / FOMO
    (r'(son\s*\d+\s*(saat|gün|yer|slot))', 12, "Aciliyet yaratma"),
    (r'(fırsatı\s*kaçırma|son\s*şans|sınırlı)', 12, "FOMO dili"),
    (r'(bugün\s*katıl|hemen\s*katıl|şimdi\s*yatır)', 10, "Acil eylem çağrısı"),

    # Sahte meşruiyet
    (r'(SEC|lisanslı|düzenleyici|onaylı)', 8, "Sahte lisans iddiası"),
    (r'(kanıtlandı|ispatlı|test\s*edildi)', 8, "Kanıtlanmış getiri iddiası"),

    # Bot / spam belirtileri
    (r'(ödeme\s*aldım|para\s*geldi|yatırım\s*yapın)', 6, "Bot mesaj şablonu"),
]

# Matematiksel imkânsızlık tespiti için getiri çıkarıcı
DAILY_RATE_PATTERN = re.compile(r'günde?\s*%\s*(\d+(?:[.,]\d+)?)', re.IGNORECASE)
MONTHLY_RATE_PATTERN = re.compile(r'aylık\s*%\s*(\d+(?:[.,]\d+)?)', re.IGNORECASE)


@dataclass
class SignalHit:
    description: str
    score: int
    matched_text: str
    severity: str  # high / medium / low


@dataclass
class PonziScore:
    total: int = 0
    telegram_score: int = 0
    signal_hits: list = field(default_factory=list)
    impossibility_multiplier: Optional[float] = None  # Yıllık getiri çarpanı
    verdict: str = "İzlemede"  # İzlemede / Orta Risk / Kritik

    def to_dict(self):
        return {
            "total": self.total,
            "telegram_score": self.telegram_score,
            "impossibility_multiplier": self.impossibility_multiplier,
            "verdict": self.verdict,
            "signal_hits": [
                {
                    "description": h.description,
                    "score": h.score,
                    "matched_text": h.matched_text,
                    "severity": h.severity,
                }
                for h in self.signal_hits
            ],
        }


def score_message(text: str) -> PonziScore:
    """Tek bir mesajı analiz eder, PonziScore döner."""
    result = PonziScore()
    text_lower = text.lower()

    for pattern, points, description in TELEGRAM_SIGNALS:
        match = re.search(pattern, text_lower, re.IGNORECASE)
        if match:
            severity = "high" if points >= 18 else ("medium" if points >= 10 else "low")
            result.signal_hits.append(
                SignalHit(
                    description=description,
                    score=points,
                    matched_text=match.group(0)[:60],
                    severity=severity,
                )
            )
            result.telegram_score += points

    # Cap at 100
    result.telegram_score = min(result.telegram_score, 100)
    result.total = result.telegram_score

    # Matematiksel imkânsızlık hesabı
    daily_match = DAILY_RATE_PATTERN.search(text)
    if daily_match:
        rate = float(daily_match.group(1).replace(",", "."))
        result.impossibility_multiplier = round((1 + rate / 100) ** 365, 1)

    monthly_match = MONTHLY_RATE_PATTERN.search(text)
    if monthly_match and not result.impossibility_multiplier:
        rate = float(monthly_match.group(1).replace(",", "."))
        result.impossibility_multiplier = round((1 + rate / 100) ** 12, 1)

    # Verdict
    if result.total >= 70:
        result.verdict = "Kritik"
    elif result.total >= 40:
        result.verdict = "Orta Risk"
    else:
        result.verdict = "İzlemede"

    return result


def score_group_messages(messages: list[str]) -> PonziScore:
    """Bir grubun son N mesajını toplu analiz eder."""
    combined_score = PonziScore()
    all_hits: dict[str, SignalHit] = {}

    for msg in messages:
        msg_score = score_message(msg)
        combined_score.telegram_score = min(
            100, combined_score.telegram_score + msg_score.telegram_score // max(len(messages), 1)
        )
        for hit in msg_score.signal_hits:
            # Aynı sinyal türü birden fazla mesajda tekrarlanıyorsa puan artır
            if hit.description in all_hits:
                all_hits[hit.description].score = min(
                    all_hits[hit.description].score + 5, 30
                )
            else:
                all_hits[hit.description] = hit

        if msg_score.impossibility_multiplier and not combined_score.impossibility_multiplier:
            combined_score.impossibility_multiplier = msg_score.impossibility_multiplier

    combined_score.signal_hits = list(all_hits.values())
    combined_score.total = min(100, combined_score.telegram_score)

    if combined_score.total >= 70:
        combined_score.verdict = "Kritik"
    elif combined_score.total >= 40:
        combined_score.verdict = "Orta Risk"
    else:
        combined_score.verdict = "İzlemede"

    return combined_score
