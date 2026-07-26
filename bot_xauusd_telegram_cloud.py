""
Bot Telegram per aggiornamenti su XAU/USD (Oro/Dollaro)
=========================================================
Cosa fa:
  - Controlla periodicamente il prezzo di XAU/USD e lo invia su Telegram
  - Controlla periodicamente le notizie sul mercato dell'oro e invia quelle nuove

PRIMA DI AVVIARE: compila la sezione CONFIGURAZIONE qui sotto con le tue chiavi.
Vedi le istruzioni per ottenerle nella chat dove hai ricevuto questo file.
"""

import requests
import json
import os
import time
import html
from datetime import datetime

# ============================================================
# CONFIGURAZIONE — inserisci qui le tue chiavi
# ============================================================

TELEGRAM_BOT_TOKEN = "INSERISCI_IL_TUO_TOKEN_BOTFATHER"
TELEGRAM_CHAT_ID = "INSERISCI_IL_TUO_CHAT_ID"

GOLDAPI_KEY = "INSERISCI_LA_TUA_CHIAVE_GOLDAPI"        # da https://www.goldapi.io
MARKETAUX_KEY = "INSERISCI_LA_TUA_CHIAVE_MARKETAUX"    # da https://www.marketaux.com

# Ogni quanti minuti controllare il prezzo e inviarlo
# 120 min = 12 volte/giorno = ~360 richieste/mese (piano free GoldAPI ~500/mese)
INTERVALLO_PREZZO_MIN = 120

# Ogni quanti minuti controllare le notizie
# 30 min = 48 volte/giorno (piano free Marketaux: 100 richieste/giorno)
INTERVALLO_NOTIZIE_MIN = 30

# Lingua delle notizie: "en" (inglese, più risultati), "it" (italiano), oppure "it,en"
LINGUA_NOTIZIE = "en"

# Quante notizie candidate analizzare ad ogni controllo (non consuma più richieste
# API: è sempre 1 sola chiamata, cambia solo quante ne restituisce)
NOTIZIE_DA_ANALIZZARE = 10

# Tra le candidate NUOVE, quante tra le più rilevanti inviare: le altre si scartano.
# Il "relevance_score" di Marketaux è relativo a questa ricerca, non una scala fissa:
# si prendono sempre "i migliori N trovati". Il punteggio compare in ogni messaggio,
# quindi osservando i valori reali nei primi giorni si può alzare/abbassare questo
# numero se sembra troppo permissivo o troppo severo.
NOTIZIE_IMPORTANTI_DA_INVIARE = 3

FILE_NOTIZIE_INVIATE = "notizie_inviate.json"

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

def carica_notizie_inviate():
    if os.path.exists(FILE_NOTIZIE_INVIATE):
        try:
            with open(FILE_NOTIZIE_INVIATE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError):
            return set()
    return set()


def salva_notizie_inviate(uuids):
    # tiene solo le ultime 300 per non far crescere il file all'infinito
    uuids_lista = list(uuids)[-300:]
    with open(FILE_NOTIZIE_INVIATE, "w") as f:
        json.dump(uuids_lista, f)


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
    Qui cerchiamo prima un'entità che sembra essere l'oro; se non c'è, facciamo
    la media di tutte le entità trovate; se non c'è nessuna entità, non mostriamo nulla.
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


def controlla_e_invia_notizie():
    log("Controllo notizie oro...")
    notizie_inviate = carica_notizie_inviate()
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
            time.sleep(2)  # piccola pausa tra un messaggio e l'altro

    salva_notizie_inviate(notizie_inviate)


# ============================================================
# LOOP PRINCIPALE
# ============================================================

def main():
    log("Bot XAU/USD avviato")
    log(f"Intervallo prezzo: ogni {INTERVALLO_PREZZO_MIN} minuti")
    log(f"Intervallo notizie: ogni {INTERVALLO_NOTIZIE_MIN} minuti")

    if "INSERISCI" in TELEGRAM_BOT_TOKEN or "INSERISCI" in TELEGRAM_CHAT_ID:
        log("ATTENZIONE: devi ancora configurare TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID in cima al file!")
        return

    # Primo controllo subito all'avvio
    controlla_e_invia_prezzo()
    controlla_e_invia_notizie()
    ultimo_controllo_prezzo = time.time()
    ultimo_controllo_notizie = time.time()

    while True:
        ora_attuale = time.time()

        if ora_attuale - ultimo_controllo_prezzo >= INTERVALLO_PREZZO_MIN * 60:
            controlla_e_invia_prezzo()
            ultimo_controllo_prezzo = ora_attuale

        if ora_attuale - ultimo_controllo_notizie >= INTERVALLO_NOTIZIE_MIN * 60:
            controlla_e_invia_notizie()
            ultimo_controllo_notizie = ora_attuale

        time.sleep(60)  # ricontrolla ogni minuto se è ora di agire


if __name__ == "__main__":
    main()
