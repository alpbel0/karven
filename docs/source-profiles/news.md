# Kaynak Profili: Haber RSS akışları (Task 1.7)

- Profil tarihi: 03.10.2026
- Kapsam: Beş RSS ekonomi akışı ve bunların tam metin çekimi
- Durum: Canlı doğrulandı (03.10.2026); fixture'lar gerçek cevaplardan kaydedildi

## 1. Kaynaklar

| Anahtar | Akış URL'si | Öğe sayısı (03.10.2026) |
|---|---|---|
| `sabah` | `https://www.sabah.com.tr/rss/ekonomi.xml` | 10 |
| `haberturk` | `https://www.haberturk.com/rss/ekonomi.xml` | 30 |
| `sozcu` | `https://www.sozcu.com.tr/feeds-rss-category-ekonomi` | 50 |
| `bloomberght` | `https://www.bloomberght.com/rss` | 50 |
| `cnnturk` | `https://www.cnnturk.com/feed/rss/ekonomi/news` | 35 |

Ölçüm (03.10.2026, düz Chrome `User-Agent`, WAF yok, throttle yok): 10 ardışık
haber sayfası 0,1–0,45 sn'de HTTP 200 döndü.

Gerçek fixture'lar: `backend/tests/fixtures/news/{sabah,haberturk,sozcu,bloomberght,cnnturk}.feed.raw.xml`
ve her sitenin 2. öğesinin sayfası `{...}.article.raw.html`.

## 2. Akış biçimi (ölçülen)

- Beş akış da RSS 2.0 `<item>` satırı.
- Başlık `<![CDATA[...]]>` ya da düz olabilir.
- `<link>` her beş kaynakta da makale URL'sidir ve `external_id` olarak
  **normalize edilmiş `<link>`** kullanılır (`<guid>` değil):
  - Habertürk `<link>` öğesini `<guid>`'dan önce verir.
  - CNN Türk'ün `<guid>`'ı URL değil (`6ac15ce0...`), ayrıca öğe düzeyinde fazladan
    bir `<atom:link>` vardır (yok sayılır).
  - Sözcü'nün `<description>`'ı çevresinde boşluk olan CDATA içerir (kırpılır).
- `<pubDate>` RFC 822: `Sat, 03 Oct 2026 17:28:06 +0300` (Sabah, Sözcü'de
  `&#x2B;0300`), `... GMT` (Habertürk, BloombergHT, CNN Türk).
  `email.utils.parsedate_to_datetime` ile ayrıştırılır, tz-aware UTC saklanır.
  Eksik/ayrıştırılamayan `pubDate` → `published_at = NULL` (asla uydurulmaz),
  öğe yine alınır.
- **CNN Türk saat dilimi düzeltmesi (canlı ölçüm, 03.10.2026):** CNN Türk'ün RSS
  `pubDate`'i "GMT" etiketli ama gerçekte Türkiye yerel saati (UTC+3). Makale
  sayfalarının kendi yayın zamanıyla karşılaştırıldı: RSS
  `Sat, 03 Oct 2026 22:56:02 GMT` ↔ sayfa `2026-10-03T22:56:02+03:00`;
  `21:52:54 GMT` ↔ `21:51:00+03:00`; `20:56:03 GMT` ↔ `20:56:03+03:00`. GMT
  sanılıp ayrıştırılırsa her CNN satırının `published_at`'i 3 saat ileri olur.
  Bu yüzden `PUBDATE_LOCAL_TIME_SOURCES = {"cnnturk"}` için duvar-saati sabit
  UTC+3 kabul edilip UTC'ye çevrilir (`timezone(timedelta(hours=3))`; tzdata
  gerekmesin diye `zoneinfo` yok — Türkiye 2016'dan beri yıl boyu UTC+3).
  Diğer dört kaynak aynı yöntemle doğrulandı ve etiketine göre ayrıştırılır
  (Habertürk `15:01:10 GMT` ↔ `18:00:57+03:00`, BloombergHT `18:51:00 GMT` ↔
  `21:54:18+03:00`).
- XML `xml.etree.ElementTree` ile ayrıştırılır; XML olmayan gövde, `<channel>`'ı
  olmayan gövde ya da **hiç `<item>` içermeyen** (gerçek akışlarda 10-50 öğe
  vardır) gövde tipli `FeedError`'dır. `<link>`'i olmayan öğeler atlanır ama
  sayılır (hepsi linksizse `[]` döner, hata değil). Namespace'ler bozulmadan
  işlenir.

## 3. Tam metin çıkarımı

`trafilatura.extract(html, favor_recall=True, include_comments=False)`
(trafilatura 2.3.0). Gerçek fixture'larda ölçülen karakter sayısı:

| Kaynak | Karakter |
|---|---|
| sabah | 1065 |
| haberturk | 1638 |
| sozcu | 1616 |
| bloomberght | 1823 |
| cnnturk | 1518 |

Sabah'ın metni `Giriş Tarihi: ...` / `Son Güncelleme: ...` satırlarıyla başlar;
`extract_text()` bu iki önekle başlayan **baştaki** satırları (ve baştaki boş
satırları) tüm kaynaklar için temizler. Bazı gerçek BloombergHT sayfaları yalnız
~300 karaktere çıkar.

Şimdilik site başına seçici (selector) yedeği yok; `extract_text()` tek fonksiyon
olarak bırakıldı, ileride eklenebilir.

## 4. Kurallar

- **Yoklama:** beş akış 15 dakikada bir (`NEWS_POLL_INTERVAL_SECONDS=900`).
- **İlk tur:** bir kaynak için hiç satır yoksa yalnız `pubDate`'e göre en yeni
  `NEWS_FIRST_RUN_LIMIT=4` öğe alınır. **Sonraki turlarda yalnız gerçekten yeni
  öğeler** alınır: `published_at >= baseline`, burada `baseline` o kaynağın ilk
  yoklama zamanıdır (`MIN(first_seen_at)`). İlk turda sınır yüzünden alınmayan
  eski yığın bir daha asla girmez. `published_at` yoksa öğe yine alınır (haber
  kaybolmasın) ama her yoklamada kaynak başına bir kez `logger.warning` ile
  sayısı görünür kılınır.
- **Tekrar kontrolü yok:** MVP'de kaynaklar/başlıklar arası tekrar haber tespiti
  yok; her haber işlenir. Tek dedup teknik `(source, external_id)` benzersizliğidir
  (`INSERT ... ON CONFLICT DO NOTHING`).
- **Durumlar** (`news_articles.text_status`):
  - `pending`: RSS'ten yazıldı, tam metin henüz çekilmedi.
  - `ok`: metin `NEWS_MIN_TEXT_CHARS=200` karakterden uzun.
  - `no_text`: sayfa çekildi (HTTP 200) ama çıkarım boş/kısa ya da trafilatura
    hata verdi; neden `text_error`'da (ör. `extracted 87 chars < 200`,
    `no text extracted`, `extraction error: ...`).
  - `failed`: tüm denemelerden sonra HTTP/timeout/transport hatası; neden
    `text_error`'da.
- **Denemeler:** makale isteği en çok `NEWS_MAX_ATTEMPTS=3`; yalnız
  timeout/transport/5xx tekrar denenir, 4xx kesindir. Otomatik retry yok; kuyruğa
  hiç girmeyen `pending` satır `NEWS_PENDING_REQUEUE_SECONDS=600` sonra yeniden
  kuyruğa alınır.
- **Başarısız akış** diğerlerini durdurmaz: raporlanır, loglanır ve Celery görevi
  tüm akışlar bittikten ve yeni satırlar commit edilip kuyruğa alındıktan sonra
  `NewsPollError` yükseltir.

## 5. CLI

```text
python -m app.news poll [--dry-run]
python -m app.news fetch --id N
```

- `poll --dry-run`: beş akışı çeker ve ayrıştırır, kaynak başına öğe sayısını ve
  ilk-tur kuralının kaç öğe alacağını yazar; DB'ye ve MinIO'ya **hiçbir şey**
  yazmaz.
- `poll` (dry-run olmadan): `service.poll_feeds` ile aynı yazma yolunu kullanır.
- `fetch --id N`: satırı önce `pending`'e çeker (bu yüzden `failed`/`no_text`
  satırlarda da çalışır), sonra tam metni yeniden çeker.
- Windows konsolu cp1254'tür: CLI asla ham makale metni basmaz, yalnız sayı ve
  kısa başlık yazar.

## 6. Bilinen sınırlar

- **BloombergHT genel akış:** ekonomiye özel akışı yok, tek genel RSS verir
  (`.../rss`, 50 öğe). "Ekonomik değil" diye hiçbir öğe elenmez (DECISIONS §6).
- **Seçici yedeği yok:** çıkarım yalnız trafilatura; başarısız sayfa `no_text`
  olur, kaybolmaz.
- **Tekrar haber tespiti yok** (planlı; ilerisi).
- Özet (`rss_summary`) kısa, HTML'den arındırılmış bir fragmandır; agent tam
  metni (`content_text`) okur.
