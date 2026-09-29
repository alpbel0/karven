# Faz 0 — İskelet

## Faz amacı

Yeni projenin fiziksel iskeletini sıfırdan kurmak: yeni GitHub reposu, monorepo
klasör yapısı, Python backend, boş Next.js uygulaması, Docker Compose ile ayağa
kalkan PostgreSQL/Neo4j/Redis/MinIO servisleri ve iki veritabanının şemasını
deterministik biçimde kuran ayrı bir `migrator` servisi. Bu fazda iş mantığı
yazılmaz. Eski projede sonradan pahalıya patlayan iki konu burada baştan doğru
kurulur: canlı ve test ortamının hiçbir şeyi paylaşmaması, ve ajanların
kullanacağı LLM istemcisinin bilinen sağlayıcı sorunlarına karşı dayanıklı
olması.

## Task 0.1 — Yeni GitHub reposu ve monorepo iskeleti

**Repo:** `karven` (yeni repo)
**Alan:** `root`
**Durum:** Tamamlandı (2026-09-29)
**Bağımlılıklar:** —

**Referanslar:** `docs/DECISIONS.md` §1, §12

**Hedef dosyalar:** Bu task'ta kurulan klasör yapısına göre belirlenir.

### Checklist

- [x] Yeni GitHub reposunu oluştur (eski `alpbel0/karven` reposunu kullanıcı
      siler; yeni klasörü kullanıcı açar). Eski repodan kod taşınmaz; yalnızca
      `docs/DECISIONS.md`, `REQUIREMENTS.md`, `PROJECT-OVERVIEW.md`,
      `PRODUCTION-ARCHITECTURE.md`, `docs/catalog/`, `docs/source-profiles/` ve
      `roadmap/` taşınır.
- [x] Backend: **FastAPI + Python 3.12**, paket yönetimi **uv**. Frontend:
      **Next.js + Tailwind** (boş uygulama).
- [x] Ayarları ortam değişkenlerinden okuyan yapılandırmayı kur; repoda düz
      metin anahtar bulunmaz, `.env` git dışında kalır, `.env.example` tüm
      anahtar adlarıyla eklenir. Anahtarlar eski `.env`'den taşınmaz; `.env`'i
      kullanıcı doldurur.
- [x] Backend ve frontend için boş test altyapısını kur; boş suite yeşil koşsun.
- [x] Adlandırma: tablo/sütun, dosya ve kod adları İngilizce.
- [x] CI kurulmaz (MVP'de testler yerelde); MVP bu bilgisayarda çalışır.
- [x] Git düzeni: yerel `work` dalı; push yalnız kullanıcı söyleyince, `main`'e
      squash ile tek commit (`docs/DECISIONS.md` §12b).

**Kabul kriteri:** Temiz bir klonda backend ve frontend kurulup boş testler
hatasız bitiyor; repoda hiçbir sır dosyası yok.

## Task 0.2 — Docker Compose servis topolojisi

**Repo:** `karven`
**Alan:** `infra`
**Durum:** Tamamlandı (2026-09-29)
**Bağımlılıklar:** Task 0.1

**Referanslar:** `docs/DECISIONS.md` §12, §13

**Hedef dosyalar:** Task 0.1'de kurulan yapıya göre belirlenir.

### Checklist

- [x] `postgres`, `neo4j`, `redis`, `minio` servislerini kalıcı volume'larla tanımla.
- [x] Uygulama servislerini (API, Celery worker, beat, migrator, frontend)
      tanımla; backend servisleri aynı imajı kullanır, komutları farklıdır.
- [x] Her altyapı servisine gerçek sağlık kontrolü yaz (port açık olması değil,
      sorgu/ping cevabı); bağımlılıklar buna bağlansın.
- [x] Host portlarını Windows'un rezerve ettiği aralıklarla çakışmayacak biçimde
      seç ve belgelendir (eski projede Neo4j ve API portları rezerve aralığa
      düşüp servisler açılamadı).

**Kabul kriteri:** Temiz bir makinede `docker compose up` sonrası dört altyapı
servisi de healthy durumuna geçiyor.

## Task 0.3 — `migrator` servisi: PostgreSQL + Neo4j şema versiyonlama

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Tamamlandı (2026-09-29)
**Bağımlılıklar:** Task 0.2

**Referanslar:** `docs/DECISIONS.md` §9, §12

**Hedef dosyalar:** Task 0.1'de kurulan yapıya göre belirlenir.

### Checklist

- [x] PostgreSQL şemasını Alembic migration'larıyla kur.
- [x] Neo4j constraint/index'lerini versiyonlu dosyalarla, tekrar çalıştırılınca
      bozulmayacak biçimde kur.
- [x] `migrator` ayrı bir servis olarak çalışsın; uygulama servisleri migration
      bitmeden başlamasın.

**Kabul kriteri:** Boş veritabanlarında `migrator` baştan sona çalışıyor ve
ikinci kez çalıştırıldığında hiçbir şey değiştirmeden bitiyor.

## Task 0.4 — Canlıdan tamamen ayrı test ortamı

**Repo:** `karven`
**Alan:** `infra`
**Durum:** Tamamlandı (2026-09-29)
**Bağımlılıklar:** Task 0.3

**Referanslar:** `docs/DECISIONS.md` §13

**Hedef dosyalar:** Task 0.1'de kurulan yapıya göre belirlenir.

### Checklist

- [x] Entegrasyon testleri için ayrı bir compose projesi tanımla: kendi
      volume'ları, kendi portları, kendi veritabanları.
- [x] Test imajı canlının Docker imaj etiketini **kullanmasın ve değiştirmesin**
      (eski projede test build'i canlı etiketini kaydırdı ve eski canlı imaj
      silindi).
- [x] Test ortamının canlı veritabanına bağlanmasını imkânsız kılan bir koruma
      ekle (ör. port/ad kontrolü; eski projede test ayarı canlı Postgres
      portunu gösteriyordu).
- [x] Tek komutla: ortamı kur → migration → testleri çalıştır → ortamı kapat.
- [x] Test düzeni (`docs/DECISIONS.md` §12b): entegrasyon testleri yalnız
      veritabanı / Docker / dış servis kodu değişince, faz sonunda ve push öncesinde.

**Kabul kriteri:** Canlı ortam çalışırken entegrasyon testleri koşuyor; canlı
veritabanı, portlar ve imaj etiketi test öncesi ve sonrası birebir aynı kalıyor.

## Task 0.5 — LLM istemcisi: EVREN + OpenRouter yedek

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 0.1

**Referanslar:** `docs/DECISIONS.md` §4, §13

**Hedef dosyalar:** Task 0.1'de kurulan yapıya göre belirlenir.

### Checklist

- [ ] Varsayılan model **DeepSeek v4.1 Flash**, sağlayıcı **EVREN**; EVREN
      başarısız olursa **OpenRouter** yedeğine geçiş.
- [ ] Araç (tool) döngüsü: JSON şeması **yalnızca son turda** gönderilir; araç
      turlarında gönderilmez.
- [ ] JSON cevaplarında sağlayıcının sızdırdığı şablon belirteçleri
      (`<|im_end|>` gibi) temizlenir; başka her fazlalık hata olarak kalır.
- [ ] Sağlayıcı hız sınırında (429) `Retry-After` dikkate alınarak uzun ve
      artan bekleme; toplam süre sınırlı; 400/401/404 hemen hata.
- [ ] Jev (TypeSafe; kredi bitince OpenRouter'daki Jev) için istemci.
- [ ] LLM çağrılarının ham kaydı (istek + cevap) MinIO'da saklanır; anahtarlar
      maskelenir.

**Kabul kriteri:** Sahte sağlayıcıyla birim testleri: şema yalnız son turda
gidiyor, `<|im_end|>` ekli JSON ayrıştırılıyor, `429, 429, 200` dizisi
beklemeyle başarıyla bitiyor, EVREN hatasında OpenRouter'a geçiliyor.

## Task 0.6 — Prompt'ların veritabanında sürümlü saklanması

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 0.3, Task 0.5

**Referanslar:** `docs/DECISIONS.md` §4

**Hedef dosyalar:** Task 0.1'de kurulan yapıya göre belirlenir.

### Checklist

- [ ] Ajan prompt'ları veritabanında sürümlü tutulur; eski sürümler silinmez.
- [ ] Her ajan için etkin sürüm seçilebilir; kod değişikliği gerekmeden yeni
      sürüm eklenip etkinleştirilebilir (admin ekranı Task 5.5'te).
- [ ] Her LLM çağrısının kaydında hangi prompt sürümünün kullanıldığı yazar.

**Kabul kriteri:** Bir ajana yeni prompt sürümü eklenip etkinleştirildiğinde
sonraki çağrı yeni sürümü kullanıyor; eski sürüm veritabanında duruyor.
