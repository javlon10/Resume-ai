# Production joylashtirish

Loyiha `<SERVER-IP>:8430` da HTTPS orqali ishlaydi.

- **Interfeys:** https://<SERVER-IP>:8430/
- **API hujjati:** https://<SERVER-IP>:8430/docs
- **API kalit:** `secrets/prod.env` faylida (rejim 600, git'ga tushmaydi)

---

## 1. Tezkor buyruqlar

```bash
cd /root/resume-ai/resume_analize

systemctl status resume-analize-api        # holat
systemctl restart resume-analize-api       # API'ni qayta ishga tushirish
systemctl restart resume-analize-llama     # modellarni qayta yuklash (~1 daqiqa)
journalctl -u resume-analize-api -f        # jonli log

./scripts/health.sh                        # 4 GPU + VRAM holati
curl -sk https://127.0.0.1:8430/health     # API holati
```

Server qayta yuklansa ikkala xizmat ham **avtomatik ko'tariladi**
(`systemctl enable` qilingan).

---

## 2. Nima o'zgardi

| | Ilgari (ishlab chiqish) | Hozir (production) |
|---|---|---|
| Port | 9000 | **8430** |
| Protokol | HTTP | **HTTPS** (`certs/server.crt`) |
| Kirish | ochiq | **API kalit majburiy** |
| CORS | `*` | `https://<SERVER-IP>:8430` |
| Ishga tushirish | qo'lda `nohup` | **systemd** (qayta yuklashga chidamli) |

Ishlab chiqish rejimi saqlanib qolgan: `./scripts/start_api.sh --daemon`
(9000, HTTP, kalitsiz) — lokal sinovlar uchun.

---

## 3. Kirish nazorati modeli

Ikki xil foydalanuvchi bor va ular boshqacha himoyalangan.

### HR / tizim (API kalit talab qiladi)

`X-API-Key` sarlavhasi bilan:

- rezyume baholash (`/score`, `/score/upload`, `/score/batch`)
- intervyu savol banki **kutilgan javoblari bilan** (`/interviews/{id}`)
- sessiya ochish va natijani ko'rish
- ovoz sintezi va transkripsiya (`/voice/tts`, `/voice/stt`)

Kalitsiz bu endpointlar `401` qaytaradi.

### Nomzod (kalitsiz, sessiya havolasi bilan)

Nomzodda kalit bo'lmaydi, shuning uchun sessiyaga bog'liq endpointlar ochiq:

- `GET /interview/sessions/{sid}` — savollar (**kutilgan javoblarsiz**)
- `PUT /interview/sessions/{sid}/answers` — qoralama saqlash
- `POST /interview/sessions/{sid}/submit` — yuborish va baholash
- `GET /voice/question/{sid}/{qid}` — savolni ovozda tinglash
- `POST /voice/answer/{sid}/{qid}` — ovozli javob

Ularni `session_id` himoyalaydi — 48 bitlik tasodifiy token. Havolani bilgan
kishi faqat **o'sha bitta sessiyaga** kira oladi, boshqasiga emas.

### Ochiq (himoyasiz)

`/` (interfeys), `/health`, `/jobs`, `/rubric`, `/docs`, `/voice/status`
(kalitsiz ichki manzillarsiz qisqartirilgan holda).

---

## 4. Sertifikat — brauzer ogohlantirishi

Sertifikat **o'z-o'zini imzolagan** (self-signed), shuning uchun brauzer
"ulanish xavfsiz emas" deb ogohlantiradi. Bu kutilgan holat — ma'lumot
shifrlanadi, lekin sertifikatni tashqi markaz tasdiqlamagan.

Ogohlantirishdan qutulish uchun ikki yo'l:

1. **Domen olib, Let's Encrypt sertifikati** — eng to'g'ri yechim. Domen
   `<SERVER-IP>` ga yo'naltirilsa, `certbot` bepul sertifikat beradi.
2. Sertifikatni foydalanuvchilar kompyuteriga qo'lda ishonchli qilib qo'shish —
   faqat kichik yopiq guruh uchun.

Sertifikat ma'lumoti:

```
CN = <SERVER-IP>, O = resume_analize
SAN: IP <SERVER-IP>, <LAN-IP>, <LAN-IP-2>, 127.0.0.1, DNS localhost
Amal qilish muddati: 2028-yil 19-dekabrgacha
```

Yangilash:

```bash
cd /root/resume-ai/resume_analize
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout certs/server.key -out certs/server.crt \
  -subj "/CN=<SERVER-IP>/O=resume_analize" \
  -addext "subjectAltName=IP:<SERVER-IP>,IP:127.0.0.1,DNS:localhost"
chmod 600 certs/server.key
systemctl restart resume-analize-api
```

---

## 5. API kalitni almashtirish

```bash
cd /root/resume-ai/resume_analize
NEW=$(openssl rand -hex 24)
sed -i "s|^RA_API_KEY=.*|RA_API_KEY=$NEW|" secrets/prod.env
systemctl restart resume-analize-api
echo "Yangi kalit: $NEW"
```

Kalit almashgach interfeysdan foydalanuvchilar uni qaytadan kiritishi kerak
(brauzer `localStorage` da saqlaydi).

---

## 6. Xavfsizlik holati

**Qilingan:**

- llama-serverlar faqat `127.0.0.1` da tinglaydi — tashqaridan modelga
  to'g'ridan-to'g'ri kirib bo'lmaydi
- baholash endpointlari kalit bilan yopilgan
- intervyu kutilgan javoblari kalitsiz ko'rinmaydi
- `/voice/status` kalitsiz ichki tarmoq manzillarini bermaydi
- maxfiy fayllar `600` rejimida va `.gitignore` da
- model va rezyume ma'lumoti serverdan chiqmaydi (TTS/STT bundan mustasno —
  ular tashqi xizmatlar)

**Hali qilinmagan / e'tiborda tuting:**

- Sertifikat self-signed (yuqoriga qarang)
- So'rovlar soni cheklanmagan (rate limit yo'q). Kalit sizib chiqsa,
  kimdir GPU'ni band qilib qo'yishi mumkin. Kalitni cheklangan doirada tarqating.
- Loglar aylanmaydi (logrotate sozlanmagan) — `logs/` katalogini vaqti-vaqti
  bilan tekshirib turing
- Zaxira nusxa avtomatlashtirilmagan. Muhim ma'lumot: `data/sessions/`
  (intervyu natijalari), `data/out/` (rezyume baholari), `secrets/`, `certs/`
- Nomzod javoblari **tashqi TTS/STT xizmatlariga** yuboriladi. Shaxsiy
  ma'lumot nuqtai nazaridan buni tekshirib ko'ring — kerak bo'lsa
  `RA_VOICE=0` bilan o'chiring.

---

## 7. Qayta yuklashdan keyin

Hech narsa qilish shart emas — systemd o'zi ko'taradi. Tekshirish:

```bash
systemctl status resume-analize-llama resume-analize-api
curl -sk https://127.0.0.1:8430/health
```

Modellar yuklanishi ~1 daqiqa, API shu vaqt kutadi (`ExecStartPre`).

---

## 8. Eski loyiha (nomzod-mvp-3)

8430 porti ilgari `/root/nomzod-mvp-3` (Gemini'li MVP) tomonidan ishlatilardi.
2026-09-16 da u to'xtatildi va port shu loyihaga berildi. **Eski loyihaning
kodi va ma'lumoti tegilmagan** — boshqa portda qayta ishga tushirish mumkin,
ko'rsatma: `/root/nomzod-mvp-3/RESTART-8430.md`.

---

## 9. Muammo bo'lsa

| Belgi | Tekshiruv |
|---|---|
| Sahifa ochilmaydi | `systemctl status resume-analize-api`, `journalctl -u resume-analize-api -n 50` |
| "Model javob bermadi" (503) | `./scripts/health.sh` — 4 GPU ham `ok` bo'lishi kerak |
| Baholash 401 qaytaryapti | Interfeysda API kalit kiritilganmi (yuqoridagi karta) |
| Juda sekin | `curl -sk https://127.0.0.1:8430/health` → `load` bo'limi; batch ishlayotgan bo'lishi mumkin |
| Ovoz tugmalari yo'q | `curl -sk https://127.0.0.1:8430/voice/status -H "X-API-Key: ..."` |
| GPU xotirasi to'lgan | `nvidia-smi`, keyin `systemctl restart resume-analize-llama` |
