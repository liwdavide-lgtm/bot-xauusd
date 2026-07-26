"""
Bot Telegram per XAU/USD — versione per GitHub Actions
=========================================================
Cosa fa:
  - Notizie rilevanti su oro/XAUUSD (con sentiment), ad ogni esecuzione
  - Aggiornamento prezzo SOLO quando si muove sopra una soglia (non più a orario fisso),
    con confronto vs ieri e vs 7 giorni fa
  - Sentiment dei trader retail (Myfxbook), opzionale
  - Riepilogo giornaliero di sintesi una volta al giorno
  - Avviso su Telegram se una fonte dati fallisce ripetutamente (chiave scaduta,
    quota superata...), così un problema non passa inosservato

Le chiavi si leggono dalle variabili d'ambiente (GitHub Secrets). Esegue UN controllo
e poi esce: è GitHub Actions a rilanciarlo ad intervalli regolari. Lo stato (notizie
già inviate, storico prezzi, contatori di fallimento...) è salvato in stato_bot.json,
che il workflow ricommitta nel repository ad ogni esecuzione.
"""

import requests
import json
import os
import time
import html
from datetime import datetime
from zoneinfo import ZoneInfo

# ============================================================
# CONFIGURAZIONE — letta dalle variabili d'ambiente (GitHub Secrets)
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOLDAPI_KEY = os.environ.get("GOLDAPI_KEY", "")
MARKETAUX_KEY = os.environ.get("MARKETAUX_KEY", "")

# Opzionali: se non impostati, il bot funziona lo stesso ma salta il sentiment retail
MYFXBOOK_EMAIL = os.environ.get("MYFXBOOK_EMAIL", "")
MYFXBOOK_PASSWORD = os.environ.get("MYFXBOOK_PASSWORD", "")

# Ogni quanti minuti controllare il prezzo (le notizie si controllano ad ogni
# esecuzione: è la schedule del workflow su GitHub a decidere ogni quanto gira lo script)
INTERVALLO_PREZZO_MIN = 120

# Invia un aggiornamento di prezzo solo se si è mosso di almeno questa percentuale
# dall'ULTIMO aggiornamento inviato (non dall'ultimo controllo) — evita di ricevere
# un messaggio ogni 2 ore anche quando il prezzo è fermo
SOGLIA_ALERT_PREZZO_PCT = 0.5

# Ora locale (fuso configurato sotto) in cui inviare il riepilogo giornaliero.
# Il bot lo invia alla prima esecuzione dopo aver superato quest'ora, quindi con
# un controllo ogni 30 minuti arriva entro mezz'ora da quest'orario, non al minuto esatto
ORA_RIEPILOGO_GIORNALIERO = 20
FUSO_ORARIO = "Europe/Rome"

# Dopo quanti fallimenti CONSECUTIVI di una fonte dati inviare un avviso su Telegram
SOGLIA_FALLIMENTI_CONSECUTIVI = 3

LINGUA_NOTIZIE = "en"

# Quante notizie candidate analizzare ad ogni controllo (non consuma più richieste
# API: è sempre 1 sola chiamata, cambia solo quante ne restituisce)
NOTIZIE_DA_ANALIZZARE = 10

# Tra le candidate NUOVE, quante tra le più rilevanti inviare: le altre si scartano.
# Il "relevance_score" di Marketaux è relativo a questa ricerca, non una scala fissa:
# si prendono sempre "i migliori N trovati". Il punteggio compare in ogni messaggio,
# quindi osservando i valori reali nei primi giorni si può alzare/abbassare questo numero.
NOTIZIE_IMPORTANTI_DA_INVIARE = 3

FILE_STATO = "stato_bot.json"

# ============================================================
# FUNZIONI DI BASE
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


# ============================================================
# PREZZO
# ============================================================

def ottieni_prezzo_oro():
    """Recupera il prezzo attuale di XAU/USD da GoldAPI.io. None in caso di errore."""
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


def aggiorna_storico_prezzi(stato, prezzo):
    """Registra il prezzo osservato ora, per poter calcolare confronti vs ieri/settimana
    scorsa più avanti, senza dover richiamare l'API per dati storici."""
    storico = stato.get("storico_prezzi", [])
    storico.append({"ts": time.time(), "prezzo": prezzo})
    limite = time.time() - (9 * 86400)  # tiene solo gli ultimi 9 giorni
    stato["storico_prezzi"] = [p for p in storico if p["ts"] >= limite]


def trova_prezzo_storico(stato, secondi_fa):
    """Cerca, tra i prezzi osservati e salvati in precedenza, quello più vicino a
    'secondi_fa' nel passato. None se non c'è ancora abbastanza storico."""
    storico = stato.get("storico_prezzi", [])
    if not storico:
        return None
    target = time.time() - secondi_fa
    candidati = [p for p in storico if p["ts"] <= target]
    if not candidati:
        return None
    return max(candidati, key=lambda p: p["ts"])["prezzo"]


def formatta_variazione(prezzo_attuale, prezzo_passato):
    if not prezzo_passato:
        return "n/d"
    variazione_pct = (prezzo_attuale - prezzo_passato) / prezzo_passato * 100
    segno = "+" if variazione_pct >= 0 else ""
    freccia = "📈" if variazione_pct >= 0 else "📉"
    return f"{segno}{variazione_pct:.2f}% {freccia}"


def formatta_messaggio_prezzo(dati, stato):
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

    ora = datetime.now(ZoneInfo(FUSO_ORARIO)).strftime("%d/%m/%Y %H:%M")

    testo = (
        f"{emoji} <b>XAU/USD — Oro</b>\n\n"
        f"💰 Prezzo: <b>${prezzo:,.2f}</b>\n"
        f"📊 Variazione oggi: {segno}{variazione:,.2f} ({segno}{variazione_pct:,.2f}%)\n"
    )
    if massimo is not None:
        testo += f"⬆️ Massimo oggi: ${massimo:,.2f}\n"
    if minimo is not None:
        testo += f"⬇️ Minimo oggi: ${minimo:,.2f}\n"

    prezzo_ieri = trova_prezzo_storico(stato, 24 * 3600)
    prezzo_settimana = trova_prezzo_storico(stato, 7 * 24 * 3600)
    if prezzo_ieri is not None:
        testo += f"🕐 Vs ieri: {formatta_variazione(prezzo, prezzo_ieri)}\n"
    if prezzo_settimana is not None:
        testo += f"📅 Vs 7 giorni fa: {formatta_variazione(prezzo, prezzo_settimana)}\n"

    testo += f"\n🕒 {ora}"
    return testo


def controlla_e_invia_prezzo(stato):
    log("Controllo prezzo oro...")
    dati = ottieni_prezzo_oro()
    fallimento = dati is None or "price" not in dati

    if aggiorna_fallimenti(stato, "prezzo", fallimento):
        invia_alert_fallimento(
            "prezzo (GoldAPI)",
            "la chiave GOLDAPI_KEY potrebbe essere scaduta, o hai superato la quota gratuita mensile"
        )

    if fallimento:
        log(f"Risposta inattesa da GoldAPI: {dati}")
        return

    prezzo_attuale = dati["price"]
    aggiorna_storico_prezzi(stato, prezzo_attuale)

    ultimo_inviato = stato.get("ultimo_prezzo_inviato")
    prima_volta = not ultimo_inviato
    variazione_da_ultimo_invio = (
        100.0 if prima_volta else abs(prezzo_attuale - ultimo_inviato) / ultimo_inviato * 100
    )

    if prima_volta or variazione_da_ultimo_invio >= SOGLIA_ALERT_PREZZO_PCT:
        messaggio = formatta_messaggio_prezzo(dati, stato)
        if invia_messaggio_telegram(messaggio):
            log(f"Prezzo inviato su Telegram (variazione dall'ultimo invio: {variazione_da_ultimo_invio:.2f}%)")
            stato["ultimo_prezzo_inviato"] = prezzo_attuale
    else:
        log(f"Variazione dall'ultimo invio solo {variazione_da_ultimo_invio:.2f}% (soglia {SOGLIA_ALERT_PREZZO_PCT}%): non invio")


# ============================================================
# SENTIMENT TRADER RETAIL (Myfxbook, opzionale)
# ============================================================

def ottieni_sentiment_retail():
    """Percentuale di trader retail long/short su XAUUSD secondo Myfxbook.
    Richiede login (email+password): è così che funziona l'API di Myfxbook,
    non con una API key. Fa login, legge i dati, poi fa sempre logout."""
    try:
        login = requests.get(
            "https://www.myfxbook.com/api/login.json",
            params={"email": MYFXBOOK_EMAIL, "password": MYFXBOOK_PASSWORD},
            timeout=15,
        )
        login.raise_for_status()
        login_data = login.json()
        if login_data.get("error"):
            log(f"Errore login Myfxbook: {login_data.get('message')}")
            return None

        session = login_data["session"]

        try:
            outlook = requests.get(
                "https://www.myfxbook.com/api/get-community-outlook.json",
                params={"session": session},
                timeout=15,
            )
            outlook.raise_for_status()
            outlook_data = outlook.json()
        finally:
            requests.get(
                "https://www.myfxbook.com/api/logout.json",
                params={"session": session},
                timeout=15,
            )

        if outlook_data.get("error"):
            log(f"Errore community-outlook Myfxbook: {outlook_data.get('message')}")
            return None

        for simbolo in outlook_data.get("symbols", []):
            if (simbolo.get("name") or "").upper() == "XAUUSD":
                return simbolo

        log("XAUUSD non trovato nei dati Myfxbook (nome simbolo diverso da quello atteso?)")
        return None

    except requests.RequestException as e:
        log(f"Errore nel recupero sentiment retail: {e}")
        return None


def formatta_messaggio_sentiment_retail(dati):
    long_pct = dati.get("longPercentage")
    short_pct = dati.get("shortPercentage")
    if long_pct is None or short_pct is None:
        return None

    if long_pct >= 60:
        lettura = "⚠️ Molti trader long — lettura contrarian: possibile pressione ribassista"
    elif short_pct >= 60:
        lettura = "⚠️ Molti trader short — lettura contrarian: possibile pressione rialzista"
    else:
        lettura = "Posizionamento abbastanza equilibrato"

    return (
        f"👥 <b>XAU/USD — Sentiment trader retail</b>\n\n"
        f"🟢 Long: {long_pct:.0f}%\n"
        f"🔴 Short: {short_pct:.0f}%\n"
        f"{lettura}\n\n"
        f"⚠️ Dati aggregati dai trader Myfxbook: non prevedono il prezzo, "
        f"vanno letti come un indizio in più insieme al resto."
    )


def controlla_e_invia_sentiment_retail(stato):
    if not MYFXBOOK_EMAIL or not MYFXBOOK_PASSWORD:
        log("MYFXBOOK_EMAIL/MYFXBOOK_PASSWORD non configurati: salto il sentiment retail")
        return

    log("Controllo sentiment trader retail...")
    dati = ottieni_sentiment_retail()
    fallimento = dati is None

    if aggiorna_fallimenti(stato, "sentiment_retail", fallimento):
        invia_alert_fallimento(
            "sentiment retail (Myfxbook)",
            "email/password potrebbero essere sbagliate, oppure l'account è stato bloccato"
        )

    if fallimento:
        return

    messaggio = formatta_messaggio_sentiment_retail(dati)
    if messaggio and invia_messaggio_telegram(messaggio):
        log("Sentiment retail inviato su Telegram")


# ============================================================
# NOTIZIE
# ============================================================

def ottieni_notizie_oro():
    """Recupera notizie recenti su oro/XAUUSD da Marketaux.
    Ritorna una lista (anche vuota se non ce ne sono di nuove: non è un errore),
    oppure None se la chiamata stessa è fallita — la distinzione serve per non
    confondere 'nessuna notizia trovata' con 'la fonte dati non funziona'."""
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
            return None
        return risposta.get("data", [])
    except requests.RequestException as e:
        log(f"Errore nel recupero notizie: {e}")
        return None


def calcola_sentiment_notizia(articolo):
    """Marketaux restituisce il sentiment per ogni 'entità' identificata nell'articolo
    (es. XAU, TSLA...), non un punteggio unico per l'articolo intero. Cerchiamo prima
    un'entità che sembra essere l'oro; se non c'è, facciamo la media di tutte le
    entità trovate; se non c'è nessuna entità, non mostriamo nulla.
    Ritorna: (punteggio da -1 a +1 oppure None, "oro" oppure "generale")"""
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
    articoli = ottieni_notizie_oro()
    fallimento = articoli is None

    if aggiorna_fallimenti(stato, "notizie", fallimento):
        invia_alert_fallimento(
            "notizie (Marketaux)",
            "la chiave MARKETAUX_KEY potrebbe essere scaduta, o hai superato la quota gratuita giornaliera"
        )

    if fallimento:
        return

    notizie_inviate = set(stato.get("notizie_inviate", []))
    nuove = [a for a in articoli if a.get("uuid") not in notizie_inviate]

    if not nuove:
        log("Nessuna notizia nuova.")
        return

    nuove.sort(key=lambda a: a.get("relevance_score") or 0, reverse=True)
    importanti = nuove[:NOTIZIE_IMPORTANTI_DA_INVIARE]
    scartate = len(nuove) - len(importanti)
    if scartate > 0:
        log(f"{scartate} notizie nuove scartate (rilevanza troppo bassa)")

    for articolo in importanti:  # dalla più rilevante alla meno rilevante
        messaggio = formatta_messaggio_notizia(articolo)
        if invia_messaggio_telegram(messaggio):
            notizie_inviate.add(articolo.get("uuid"))
            stato["notizie_inviate_oggi"] = stato.get("notizie_inviate_oggi", 0) + 1
            log(f"Notizia inviata (rilevanza {articolo.get('relevance_score') or 0:.1f}): {articolo.get('title', '')[:60]}")
            time.sleep(2)

    stato["notizie_inviate"] = list(notizie_inviate)[-300:]


# ============================================================
# AFFIDABILITÀ — avviso se una fonte dati fallisce ripetutamente
# ============================================================

def aggiorna_fallimenti(stato, fonte, non_riuscito):
    """Aggiorna il contatore di fallimenti consecutivi per una fonte dati.
    Ritorna True esattamente quando questa chiamata fa scattare la soglia di
    allarme, così l'avviso viene inviato una volta sola e non ripetuto ad ogni
    esecuzione finché il problema persiste."""
    fallimenti = stato.setdefault("fallimenti_consecutivi", {})
    if non_riuscito:
        fallimenti[fonte] = fallimenti.get(fonte, 0) + 1
        return fallimenti[fonte] == SOGLIA_FALLIMENTI_CONSECUTIVI
    else:
        fallimenti[fonte] = 0
        return False


def invia_alert_fallimento(fonte, dettaglio):
    testo = (
        f"⚠️ <b>Attenzione: possibile problema col bot</b>\n\n"
        f"La fonte dati \"{fonte}\" ha fallito {SOGLIA_FALLIMENTI_CONSECUTIVI} volte di fila.\n"
        f"Possibile causa: {dettaglio}\n\n"
        f"Controlla i log su GitHub → Actions per i dettagli.\n\n"
        f"<i>Nota: se il problema fosse invece Telegram stesso (token revocato, bot bloccato), "
        f"questo avviso non potrebbe arrivarti — in quel caso l'unico segnale visibile è "
        f"l'esecuzione fallita (❌) nella tab Actions.</i>"
    )
    if invia_messaggio_telegram(testo):
        log(f"Alert di fallimento inviato per: {fonte}")
    else:
        log(f"Non sono riuscito a inviare l'alert di fallimento per {fonte} (Telegram stesso non risponde?)")


# ============================================================
# RIEPILOGO GIORNALIERO
# ============================================================

def dovrebbe_inviare_riepilogo(stato):
    ora_locale = datetime.now(ZoneInfo(FUSO_ORARIO))
    oggi = ora_locale.strftime("%Y-%m-%d")
    if stato.get("ultimo_riepilogo_giorno") == oggi:
        return False
    return ora_locale.hour >= ORA_RIEPILOGO_GIORNALIERO


def invia_riepilogo_giornaliero(stato):
    log("Invio riepilogo giornaliero...")
    dati = ottieni_prezzo_oro()
    if not dati or "price" not in dati:
        log("Impossibile comporre il riepilogo: prezzo non disponibile in questo momento")
        return

    prezzo = dati["price"]
    prezzo_ieri = trova_prezzo_storico(stato, 24 * 3600)
    prezzo_settimana = trova_prezzo_storico(stato, 7 * 24 * 3600)
    notizie_oggi = stato.get("notizie_inviate_oggi", 0)
    ora_locale = datetime.now(ZoneInfo(FUSO_ORARIO)).strftime("%d/%m/%Y %H:%M")

    testo = (
        f"🌇 <b>Riepilogo giornaliero — XAU/USD</b>\n\n"
        f"💰 Prezzo attuale: <b>${prezzo:,.2f}</b>\n"
    )
    if prezzo_ieri is not None:
        testo += f"🕐 Vs ieri: {formatta_variazione(prezzo, prezzo_ieri)}\n"
    if prezzo_settimana is not None:
        testo += f"📅 Vs 7 giorni fa: {formatta_variazione(prezzo, prezzo_settimana)}\n"
    testo += f"📰 Notizie rilevanti inviate oggi: {notizie_oggi}\n"

    if MYFXBOOK_EMAIL and MYFXBOOK_PASSWORD:
        sentiment = ottieni_sentiment_retail()
        if sentiment and sentiment.get("longPercentage") is not None:
            testo += f"👥 Trader retail: {sentiment['longPercentage']:.0f}% long / {sentiment['shortPercentage']:.0f}% short\n"

    testo += f"\n🕒 {ora_locale}"

    if invia_messaggio_telegram(testo):
        stato["ultimo_riepilogo_giorno"] = datetime.now(ZoneInfo(FUSO_ORARIO)).strftime("%Y-%m-%d")
        stato["notizie_inviate_oggi"] = 0
        log("Riepilogo giornaliero inviato")


# ============================================================
# STATO (persistito nel repository tra un'esecuzione e l'altra)
# ============================================================

def carica_stato():
    if os.path.exists(FILE_STATO):
        try:
            with open(FILE_STATO, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "notizie_inviate": [],
        "ultimo_controllo_prezzo": 0,
        "storico_prezzi": [],
        "ultimo_prezzo_inviato": None,
        "ultimo_riepilogo_giorno": None,
        "notizie_inviate_oggi": 0,
        "fallimenti_consecutivi": {},
    }


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
        controlla_e_invia_prezzo(stato)
        controlla_e_invia_sentiment_retail(stato)
        stato["ultimo_controllo_prezzo"] = time.time()
    else:
        log(f"Prezzo controllato {minuti_da_ultimo_prezzo:.0f} min fa: non ancora ora (ogni {INTERVALLO_PREZZO_MIN} min)")

    if dovrebbe_inviare_riepilogo(stato):
        invia_riepilogo_giornaliero(stato)

    salva_stato(stato)
    log("Controllo completato")


if __name__ == "__main__":
    main()
