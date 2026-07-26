"""
Bot Telegram per XAU/USD — versione per GitHub Actions
=========================================================
Stessa logica della versione locale, adattata per girare come workflow
programmato su GitHub invece che come processo continuo sul PC.

Differenze rispetto alla versione locale:
  - Le chiavi si leggono dalle variabili d'ambiente (GitHub Secrets),
    NON sono scritte nel file. Così il repository può restare pubblico
    (minuti Actions illimitati) senza esporre nulla.
  - Esegue UN controllo e poi esce, invece del ciclo infinito: è il
    programma di GitHub Actions a rilanciarlo ad intervalli regolari.
  - Lo stato (notizie già inviate, orario ultimo controllo prezzo) è
    salvato in stato_bot.json, che il workflow ricommitta nel repository
    ad ogni esecuzione, così sopravvive da un'esecuzione all'altra.
"""

import requests
import json
import os
import time
import html
from datetime import datetime

# ============================================================
# CONFIGURAZIONE — letta dalle variabili d'ambiente (GitHub Secrets)
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOLDAPI_KEY = os.environ.get("GOLDAPI_KEY", "")
MARKETAUX_KEY = os.environ.get("MARKETAUX_KEY", "")

# Ogni quanti minuti controllare il prezzo (le notizie si controllano ad ogni esecuzione:
# è la schedule del workflow su GitHub a decidere ogni quanto gira lo script)
INTERVALLO_PREZZO_MIN = 120

LINGUA_NOTIZIE = "en"

# Quante notizie candidate analizzare ad ogni controllo (aumentarlo non consuma più
# richieste API: è sempre 1 sola chiamata, cambia solo quante ne restituisce)
NOTIZIE_DA_ANALIZZARE = 10

# Tra le candidate NUOVE (mai inviate prima), quante tra le più rilevanti inviare
# davvero: le altre vengono scartate. Marketaux dà un "relevance_score" per ogni
# articolo rispetto alla ricerca (quanto è centrato su oro/XAUUSD, non solo citato
# di striscio) — non è una scala fissa 0-100, è relativo a questa ricerca specifica,
# quindi qui si prendono sempre "i migliori N tra quelli trovati", non un punteggio
# minimo assoluto. Il punteggio compare in ogni messaggio: osservando i valori reali
# nei primi giorni si può alzare o abbassare questo numero se sembra troppo permissivo
# o troppo severo.
NOTIZIE_IMPORTANTI_DA_INVIARE = 3

FILE_STATO = "stato_bot.json"

# ============================================================
# FUNZIONI DI SUPPORTO
# ============================================================

def log(msg):
    ora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print(f"[{ora}] {msg}")


def invia_messaggio_telegram(testo):
    """Invia un messaggio al chat/canale Telegram configurato."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": testo,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"Errore invio Telegram: {e}")
        if "r" in locals():
            log(f"   Risposta Telegram: {r.text}")
        return False


# ---------- PREZZO ----------

def ottieni_prezzo_oro():
    """Recupera il prezzo attuale di XAU/USD da GoldAPI.io"""
    url = "https://www.goldapi.io/api/XAU/USD"
    headers = {"x-access-token": GOLDAPI_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log(f"Errore nel recupero prezzo: {e}")
        if "r" in locals():
            log(f"   Risposta GoldAPI: {r.text}")
        return None


def formatta_messaggio_prezzo(dati):
    prezzo = dati["price"]
    variazione = dati.get("ch", 0)
    variazione_pct = dati.get("chp", 0)
    massimo = dati.get("high_price")
    minimo = dati.get("low_price")

    if variazione > 0:
        emoji, segno = "🟢📈", "+"
    elif variazione < 0:
        emoji, segno = "🔴📉", ""
    else:
        emoji, segno = "⚪️", ""

    ora = datetime.now().strftime("%d/%m/%Y %H:%M")

    testo = (
        f"{emoji} <b>XAU/USD — Oro</b>\n\n"
        f"💰 Prezzo: <b>${prezzo:,.2f}</b>\n"
        f"📊 Variazione: {segno}{variazione:,.2f} ({segno}{variazione_pct:,.2f}%)\n"
    )
    if massimo is not None:
        testo += f"⬆️ Massimo: ${massimo:,.2f}\n"
    if minimo is not None:
        testo += f"⬇️ Minimo: ${minimo:,.2f}\n"
    testo += f"\n🕒 {ora}"
    return testo


def controlla_e_invia_prezzo():
    log("Controllo prezzo oro...")
    dati = ottieni_prezzo_oro()
    if dati and "price" in dati:
        messaggio = formatta_messaggio_prezzo(dati)
        if invia_messaggio_telegram(messaggio):
            log("Prezzo inviato su Telegram")
    else:
        log(f"Risposta inattesa da GoldAPI: {dati}")


# ---------- NOTIZIE ----------

def ottieni_notizie_oro():
    """Recupera notizie recenti su oro/XAUUSD da Marketaux"""
    url = "https://api.marketaux.com/v1/news/all"
    params = {
        "api_token": MARKETAUX_KEY,
        "search": 'gold|XAUUSD|"gold price"',
        "language": LINGUA_NOTIZIE,
        "limit": NOTIZIE_DA_ANALIZZARE,
        "sort": "relevance_score",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        risposta = r.json()
        if "error" in risposta:
            log(f"Errore API Marketaux: {risposta['error']}")
            return []
        return risposta.get("data", [])
    except requests.RequestException as e:
        log(f"Errore nel recupero notizie: {e}")
        return []


def calcola_sentiment_notizia(articolo):
    """
    Marketaux restituisce il sentiment per ogni 'entità' identificata nell'articolo
    (es. XAU, TSLA...), non un punteggio unico per l'articolo intero.
    Cerchiamo prima un'entità che sembra essere l'oro; se non c'è, facciamo la
    media di tutte le entità trovate; se non c'è nessuna entità, non mostriamo nulla.
    Ritorna: (punteggio da -1 a +1 oppure None, "oro" oppure "generale")
    """
    entities = articolo.get("entities", [])
    if not entities:
        return None, None

    for e in entities:
        simbolo = (e.get("symbol") or "").upper()
        nome = (e.get("name") or "").lower()
        if "XAU" in simbolo or "gold" in nome:
            return e.get("sentiment_score"), "oro"

    punteggi = [e.get("sentiment_score") for e in entities if e.get("sentiment_score") is not None]
    if punteggi:
        return sum(punteggi) / len(punteggi), "generale"

    return None, None


def formatta_messaggio_notizia(articolo):
    titolo = html.escape(articolo.get("title", "Senza titolo"))
    fonte = html.escape(articolo.get("source", ""))
    link = articolo.get("url", "")
    pubblicato = articolo.get("published_at", "")[:16].replace("T", " ")

    rilevanza = articolo.get("relevance_score")

    testo = (
        f"📰 <b>{titolo}</b>\n"
        f"🏷️ Fonte: {fonte}\n"
    )

    if rilevanza is not None:
        testo += f"⭐ Rilevanza: {rilevanza:.1f}\n"

    punteggio, tipo = calcola_sentiment_notizia(articolo)
    if punteggio is not None:
        if punteggio > 0:
            etichetta_sent = "🟢 Positivo"
        elif punteggio < 0:
            etichetta_sent = "🔴 Negativo"
        else:
            etichetta_sent = "⚪️ Neutro"
        descrizione = "oro" if tipo == "oro" else "generale articolo"
        testo += f"🎭 Sentiment ({descrizione}): {etichetta_sent} ({punteggio:+.2f})\n"

    testo += (
        f"🕒 {pubblicato}\n"
        f"🔗 {link}"
    )
    return testo


def controlla_e_invia_notizie(stato):
    log("Controllo notizie oro...")
    notizie_inviate = set(stato.get("notizie_inviate", []))
    articoli = ottieni_notizie_oro()

    nuove = [a for a in articoli if a.get("uuid") not in notizie_inviate]

    if not nuove:
        log("Nessuna notizia nuova.")
        return

    # tiene solo le più rilevanti tra le nuove, scarta il resto
    nuove.sort(key=lambda a: a.get("relevance_score") or 0, reverse=True)
    importanti = nuove[:NOTIZIE_IMPORTANTI_DA_INVIARE]
    scartate = len(nuove) - len(importanti)
    if scartate > 0:
        log(f"{scartate} notizie nuove scartate (rilevanza troppo bassa)")

    for articolo in importanti:  # dalla più rilevante alla meno rilevante
        messaggio = formatta_messaggio_notizia(articolo)
        if invia_messaggio_telegram(messaggio):
            notizie_inviate.add(articolo.get("uuid"))
            log(f"Notizia inviata (rilevanza {articolo.get('relevance_score') or 0:.1f}): {articolo.get('title', '')[:60]}")
            time.sleep(2)

    # tiene solo le ultime 300 per non far crescere il file all'infinito
    stato["notizie_inviate"] = list(notizie_inviate)[-300:]


# ---------- STATO (persistito nel repository tra un'esecuzione e l'altra) ----------

def carica_stato():
    if os.path.exists(FILE_STATO):
        try:
            with open(FILE_STATO, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"notizie_inviate": [], "ultimo_controllo_prezzo": 0}


def salva_stato(stato):
    with open(FILE_STATO, "w") as f:
        json.dump(stato, f)


# ============================================================
# ESECUZIONE SINGOLA (chiamata ad ogni run del workflow GitHub Actions)
# ============================================================

def main():
    log("Bot XAU/USD (GitHub Actions) — controllo singolo")

    mancanti = [nome for nome, val in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "GOLDAPI_KEY": GOLDAPI_KEY,
        "MARKETAUX_KEY": MARKETAUX_KEY,
    }.items() if not val]
    if mancanti:
        log(f"Secrets mancanti su GitHub (Settings > Secrets and variables > Actions): {', '.join(mancanti)}")
        raise SystemExit(1)

    stato = carica_stato()

    controlla_e_invia_notizie(stato)

    minuti_da_ultimo_prezzo = (time.time() - stato.get("ultimo_controllo_prezzo", 0)) / 60
    if minuti_da_ultimo_prezzo >= INTERVALLO_PREZZO_MIN:
        controlla_e_invia_prezzo()
        stato["ultimo_controllo_prezzo"] = time.time()
    else:
        log(f"Prezzo controllato {minuti_da_ultimo_prezzo:.0f} min fa: non ancora ora (ogni {INTERVALLO_PREZZO_MIN} min)")

    salva_stato(stato)
    log("Controllo completato")


if __name__ == "__main__":
    main()
