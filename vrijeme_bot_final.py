# -*- coding: utf-8 -*-
"""
VREMENSKI BOT za Nedelisce - pokrece se RUCNO komandom kad zatrebas izvjestaj:

    python vrijeme_bot.py        (ili "python vrijeme_bot.py vr")
        -> puni vremenski izvjestaj: temperatura, UV indeks, izlazak/zalazak
           Sunca i Mjeseca, periodi padalina

    python vrijeme_bot.py aqi
        -> trenutna kvaliteta zraka (AQI) + vrijednost za sljedeci sat

KAKO RADI:
1. Skripta dohvati vremensku/AQI prognozu s Open-Meteo servisa (besplatno, bez API kljuca)
2. Izracuna izlazak/zalazak Mjeseca lokalno pomocu astronomske biblioteke "ephem"
   (Mjesecevi podaci se ne uzimaju s interneta nego se racunaju matematicki za tocnu lokaciju)
3. Sve to sastavi u preglednu poruku i posalje na Telegram

PRIJE POKRETANJA MORAS POPUNITI dolje "TELEGRAM_TOKEN" i "TELEGRAM_CHAT_ID"

INSTALACIJA (jednom, u Command Promptu):
    python -m pip install requests ephem tzdata
"""

import requests
import datetime
import os
import sys

try:
    import ephem
except ImportError:
    raise SystemExit(
        "Nedostaje biblioteka 'ephem'. Otvori Command Prompt i upisi:\n"
        "    python -m pip install ephem tzdata\n"
        "pa ponovno pokreni ovu skriptu."
    )

try:
    from zoneinfo import ZoneInfo
except ImportError:
    raise SystemExit("Ova skripta treba Python 3.9 ili noviji (zbog 'zoneinfo').")


# ============================================================
#  PODESI OVO PRIJE PRVOG POKRETANJA
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# Napomena: ako pokrecses ovo preko GitHub Actions, TELEGRAM_TOKEN i TELEGRAM_CHAT_ID
# se citaju iz GitHub Secrets (sigurno, nikad vidljivo u kodu). Ako pokrecses lokalno na
# svom racunalu, mozes gornje redove slobodno zamijeniti direktnim upisom u navodnicima.

# Koordinate Nedelisca (Medjimurska zupanija)
LATITUDE = 46.3763
LONGITUDE = 16.3855
NADMORSKA_VISINA = 168  # metara, koristi se za tocniji izracun izlaska/zalaska Mjeseca

VREMENSKA_ZONA = "Europe/Zagreb"


# ============================================================
#  OD OVDJE NADALJE NE MORAS NISTA MIJENJATI
# ============================================================

def posalji_telegram_poruku(tekst):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        odgovor = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": tekst,
        }, timeout=15)
        if odgovor.status_code != 200:
            print("GRESKA pri slanju Telegram poruke:", odgovor.text)
        else:
            print("Poruka uspjesno poslana.")
    except Exception as e:
        print("GRESKA - ne mogu poslati Telegram poruku:", e)


def dohvati_vremenske_podatke():
    """Dohvaca danasnju prognozu s Open-Meteo (besplatno, bez API kljuca).
    Uz dnevne vrijednosti, dohvacamo i SATNE vrijednosti UV indeksa i padalina,
    da bismo mogli izracunati TOCNO KADA UV prelazi/pada ispod 5 i kada padaju padaline."""
    url = "https://api.open-meteo.com/v1/forecast"
    parametri = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min,uv_index_max,precipitation_sum,sunrise,sunset",
        "hourly": "uv_index,precipitation",
        "timezone": VREMENSKA_ZONA,
        "forecast_days": 1,
    }
    odgovor = requests.get(url, params=parametri, timeout=30)
    odgovor.raise_for_status()
    return odgovor.json()


def formatiraj_vrijeme_iso(iso_tekst):
    """Pretvori Open-Meteo ISO vrijeme (npr. '2026-07-07T05:32') u citljiv oblik '05:32'."""
    if not iso_tekst:
        return "nepoznato"
    try:
        return iso_tekst.split("T")[1]
    except IndexError:
        return iso_tekst


def formatiraj_sat_iso(iso_tekst):
    """Iz '2026-07-07T14:00' izvuci samo vrijeme '14:00'."""
    try:
        return iso_tekst.split("T")[1]
    except (IndexError, AttributeError):
        return "?"


def _dodaj_minute(vrijeme_iso, minute_dodati):
    """Doda odredjen broj minuta na ISO vrijeme (npr. '2026-07-07T14:00') i vrati citljiv '14:23'."""
    t = datetime.datetime.fromisoformat(vrijeme_iso)
    t2 = t + datetime.timedelta(minutes=minute_dodati)
    return t2.strftime("%H:%M")


def pronadji_uv_prijelaze(vremena, uv_vrijednosti, prag=5.0):
    """
    Prodje kroz satne UV vrijednosti i pronadje TOCNO (linearnom interpolacijom izmedju
    dva sata) PRVI trenutak kad UV indeks prelazi iznad praga tijekom dana, i ZADNJI
    trenutak kad se spusta ispod njega (ignorira manje fluktuacije/oscilacije izmedju).
    Vraca listu s najvise 2 poruke: ["UV prelazi 5 oko 09:42", "UV pada ispod 5 oko 18:07"].
    """
    prvi_rast = None
    zadnji_pad = None

    for i in range(len(uv_vrijednosti) - 1):
        v1, v2 = uv_vrijednosti[i], uv_vrijednosti[i + 1]
        if v1 is None or v2 is None:
            continue

        # UV RASTE i prelazi prag izmedju ova dva sata
        if v1 < prag <= v2 and prvi_rast is None:
            udio = (prag - v1) / (v2 - v1) if v2 != v1 else 0
            prvi_rast = _dodaj_minute(vremena[i], udio * 60)

        # UV PADA i spusta se ispod praga izmedju ova dva sata
        elif v1 >= prag > v2:
            udio = (v1 - prag) / (v1 - v2) if v1 != v2 else 0
            zadnji_pad = _dodaj_minute(vremena[i], udio * 60)

    poruke = []
    if prvi_rast:
        poruke.append(f"UV prelazi {prag:g} oko {prvi_rast}")
    if zadnji_pad:
        poruke.append(f"UV pada ispod {prag:g} oko {zadnji_pad}")

    return poruke


def pronadji_periode_padalina(vremena, padaline_vrijednosti, prag_mm=0.1):
    """
    Grupira uzastopne sate u kojima se ocekuju padaline (iznad praga) u periode,
    i vraca listu poruka poput ["Padaline od 13:00 do 16:00 (ukupno ~4.2 mm)"].
    """
    poruke = []
    n = len(padaline_vrijednosti)
    i = 0
    while i < n:
        if padaline_vrijednosti[i] is not None and padaline_vrijednosti[i] > prag_mm:
            pocetak_idx = i
            ukupno_mm = 0.0
            while i < n and padaline_vrijednosti[i] is not None and padaline_vrijednosti[i] > prag_mm:
                ukupno_mm += padaline_vrijednosti[i]
                i += 1
            kraj_idx = i  # prvi sat NAKON zadnjeg kisnog sata (dakle "do" tog vremena)

            pocetak_tekst = formatiraj_sat_iso(vremena[pocetak_idx])
            if kraj_idx < n:
                kraj_tekst = formatiraj_sat_iso(vremena[kraj_idx])
            else:
                kraj_tekst = "kraja dana"

            poruke.append(f"Padaline od {pocetak_tekst} do {kraj_tekst} (ukupno ~{ukupno_mm:.1f} mm)")
        else:
            i += 1

    return poruke



def izracunaj_mjesec():
    """
    Izracunaj sljedeci izlazak i zalazak Mjeseca za Nedelisce, koristeci
    astronomsku biblioteku ephem (lokalni izracun, ne treba internet).
    Vraca (izlazak_lokalno, zalazak_lokalno) kao citljive stringove.
    """
    opservator = ephem.Observer()
    opservator.lat = str(LATITUDE)
    opservator.lon = str(LONGITUDE)
    opservator.elevation = NADMORSKA_VISINA
    opservator.date = datetime.datetime.utcnow()

    mjesec = ephem.Moon()
    zona = ZoneInfo(VREMENSKA_ZONA)

    try:
        sljedeci_izlazak_utc = opservator.next_rising(mjesec).datetime()
        sljedeci_izlazak_lokalno = sljedeci_izlazak_utc.replace(tzinfo=datetime.timezone.utc).astimezone(zona)
        izlazak_tekst = sljedeci_izlazak_lokalno.strftime("%d.%m. u %H:%M")
    except Exception:
        izlazak_tekst = "nije moguce izracunati"

    try:
        sljedeci_zalazak_utc = opservator.next_setting(mjesec).datetime()
        sljedeci_zalazak_lokalno = sljedeci_zalazak_utc.replace(tzinfo=datetime.timezone.utc).astimezone(zona)
        zalazak_tekst = sljedeci_zalazak_lokalno.strftime("%d.%m. u %H:%M")
    except Exception:
        zalazak_tekst = "nije moguce izracunati"

    return izlazak_tekst, zalazak_tekst


def sastavi_i_posalji_izvjestaj():
    """Dohvati sve podatke, sastavi poruku i posalji je na Telegram. Ovo je 'srce' bota -
    koristi se i za jutarnji automatski izvjestaj i za izvjestaj na zahtjev (komanda u chatu)."""
    print("Dohvacam vremenske podatke...")
    try:
        podaci = dohvati_vremenske_podatke()
    except Exception as e:
        print("Ne mogu dohvatiti vremenske podatke:", e)
        posalji_telegram_poruku("⚠️ Vremenski bot: trenutno ne mogu dohvatiti prognozu za Nedelisce. Pokusaj ponovno za koju minutu.")
        return

    trenutna_temp = podaci.get("current", {}).get("temperature_2m", "nepoznato")

    dnevno = podaci.get("daily", {})
    temp_max = dnevno.get("temperature_2m_max", ["nepoznato"])[0]
    temp_min = dnevno.get("temperature_2m_min", ["nepoznato"])[0]
    uv_max = dnevno.get("uv_index_max", ["nepoznato"])[0]
    padaline_ukupno = dnevno.get("precipitation_sum", ["nepoznato"])[0]
    izlazak_sunca = formatiraj_vrijeme_iso(dnevno.get("sunrise", [None])[0])
    zalazak_sunca = formatiraj_vrijeme_iso(dnevno.get("sunset", [None])[0])

    satno = podaci.get("hourly", {})
    vremena_satna = satno.get("time", [])
    uv_satni = satno.get("uv_index", [])
    padaline_satne = satno.get("precipitation", [])

    uv_prijelazi = pronadji_uv_prijelaze(vremena_satna, uv_satni, prag=5.0)
    periodi_padalina = pronadji_periode_padalina(vremena_satna, padaline_satne)

    uv_tekst = "\n".join(f"  • {p}" for p in uv_prijelazi) if uv_prijelazi else "  • UV danas ne prelazi 5 (ili je stalno iznad/ispod)"
    padaline_tekst = "\n".join(f"  • {p}" for p in periodi_padalina) if periodi_padalina else "  • Padaline se danas ne ocekuju"

    print("Racunam izlazak/zalazak Mjeseca...")
    izlazak_mjeseca, zalazak_mjeseca = izracunaj_mjesec()

    danas = datetime.date.today().strftime("%d.%m.%Y")
    sada = datetime.datetime.now(ZoneInfo(VREMENSKA_ZONA)).strftime("%H:%M")

    poruka = (
        f"🌤️ VREMENSKI IZVJESTAJ - Nedelisce ({danas}, {sada})\n\n"
        f"🌡️ Temperatura sada: {trenutna_temp}°C\n"
        f"🌡️ Danas min/maks: {temp_min}°C / {temp_max}°C\n\n"
        f"☀️ UV indeks (najveci danas): {uv_max}\n"
        f"{uv_tekst}\n\n"
        f"🌧️ Padaline danas (ukupno ~{padaline_ukupno} mm):\n"
        f"{padaline_tekst}\n\n"
        f"🌅 Izlazak Sunca: {izlazak_sunca}\n"
        f"🌇 Zalazak Sunca: {zalazak_sunca}\n\n"
        f"🌙 Sljedeci izlazak Mjeseca: {izlazak_mjeseca}\n"
        f"🌑 Sljedeci zalazak Mjeseca: {zalazak_mjeseca}\n"
    )

    print(poruka)
    posalji_telegram_poruku(poruka)


# ============================================================
#  KVALITETA ZRAKA (AQI)
# ============================================================

def dohvati_kvalitetu_zraka():
    """Dohvaca trenutnu i satnu europsku AQI vrijednost s Open-Meteo Air Quality API-ja
    (besplatno, bez API kljuca - isti pouzdani izvor kao i za vrijeme)."""
    url = "https://air-quality-api.open-meteo.com/v1/air-quality"
    parametri = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "european_aqi",
        "hourly": "european_aqi",
        "timezone": VREMENSKA_ZONA,
        "forecast_days": 1,
    }
    odgovor = requests.get(url, params=parametri, timeout=30)
    odgovor.raise_for_status()
    return odgovor.json()


def kategorija_aqi(vrijednost):
    """Pretvori brojku europskog AQI indeksa (0-100+) u opisnu kategoriju.
    Napomena: ovo su priblizne granice sluzbene EEA (europske) ljestvice."""
    if vrijednost is None:
        return "nepoznato"
    if vrijednost <= 20:
        return "Dobra 🟢"
    elif vrijednost <= 40:
        return "Zadovoljavajuca 🟡"
    elif vrijednost <= 60:
        return "Umjerena 🟠"
    elif vrijednost <= 80:
        return "Losa 🔴"
    elif vrijednost <= 100:
        return "Vrlo losa 🟣"
    else:
        return "Izrazito losa ⚫"


def sastavi_i_posalji_aqi():
    """Posalji trenutnu kvalitetu zraka i vrijednost za sljedeci sat."""
    print("Dohvacam podatke o kvaliteti zraka...")
    try:
        podaci = dohvati_kvalitetu_zraka()
    except Exception as e:
        print("Ne mogu dohvatiti kvalitetu zraka:", e)
        posalji_telegram_poruku("⚠️ Trenutno ne mogu dohvatiti podatke o kvaliteti zraka. Pokusaj ponovno za koju minutu.")
        return

    trenutni_aqi = podaci.get("current", {}).get("european_aqi")

    satno = podaci.get("hourly", {})
    vremena = satno.get("time", [])
    aqi_satni = satno.get("european_aqi", [])

    zona = ZoneInfo(VREMENSKA_ZONA)
    sada = datetime.datetime.now(zona)

    sljedeci_aqi = None
    sljedeci_sat_tekst = None
    for i, vrijeme_iso in enumerate(vremena):
        try:
            t = datetime.datetime.fromisoformat(vrijeme_iso).replace(tzinfo=zona)
        except ValueError:
            continue
        if t > sada:
            sljedeci_aqi = aqi_satni[i] if i < len(aqi_satni) else None
            sljedeci_sat_tekst = t.strftime("%H:%M")
            break

    poruka = (
        f"🍃 KVALITETA ZRAKA - Nedelisce\n\n"
        f"Trenutno: {trenutni_aqi} - {kategorija_aqi(trenutni_aqi)}\n"
    )
    if sljedeci_aqi is not None:
        poruka += f"Sljedeci sat ({sljedeci_sat_tekst}): {sljedeci_aqi} - {kategorija_aqi(sljedeci_aqi)}\n"

    poruka += "\n(Europski AQI indeks, 0-100+; izvor: Open-Meteo)"

    print(poruka)
    posalji_telegram_poruku(poruka)


# ============================================================
#  POKRETANJE - biraj komandu kad rucno pokrecses skriptu
# ============================================================
# python vrijeme_bot.py       -> puni vremenski izvjestaj (isto kao /vr)
# python vrijeme_bot.py vr    -> puni vremenski izvjestaj
# python vrijeme_bot.py aqi   -> kvaliteta zraka (trenutno + sljedeci sat)
# (radi i s kosom crtom: "/vr", "/aqi")

def glavna_funkcija():
    if "STAVI_SVOJ" in TELEGRAM_TOKEN or "STAVI_SVOJ" in TELEGRAM_CHAT_ID:
        print("GRESKA: Prvo moras upisati TELEGRAM_TOKEN i TELEGRAM_CHAT_ID na vrhu skripte!")
        return

    komanda = "vr"
    if len(sys.argv) > 1:
        komanda = sys.argv[1].strip().lower().lstrip("/")

    if komanda == "aqi":
        sastavi_i_posalji_aqi()
    else:
        sastavi_i_posalji_izvjestaj()


if __name__ == "__main__":
    glavna_funkcija()
