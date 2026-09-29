# Karven — Teknik Mimari (yeni proje, MVP)

> Bu doküman yalnızca **teknik/mimari** kararları içerir. Ürünün ne yapacağı
> için `docs/REQUIREMENTS.md`, kararların tamamı için `docs/DECISIONS.md`
> (çelişkide `DECISIONS.md` geçerlidir). Klasör ve dosya yapısı Faz 0'da
> belirlenir; burada yazmaz.
>
> Eski projenin mimari belgesi (iki yollu akış, outbox, ayrı kuyruklar,
> PostgreSQL'in ilişkiler için asıl kaynak olması) 2026-09-29'da yeniden
> başlamayla geçersiz oldu; git geçmişinde durur.

## 1. Ortam

- MVP **bu bilgisayarda** Docker Compose ile çalışır; site dışarı açılmaz.
  Sunucu ve alan adı MVP sonrasında seçilir. [§12]
- Kurulum tamamen yenidir: yeni GitHub reposu **`karven`**, yeni compose. [§1,
  §12]
- **Test ortamı canlıdan ayrıdır:** port, Docker imaj etiketi ve veritabanı
  paylaşılmaz. [§13]
- **CI yok;** testler yerelde çalıştırılır. [§12]

## 2. Teknoloji yığını

| Katman | Seçim |
|---|---|
| Backend | FastAPI + Python 3.12, paket yönetimi **uv** |
| Ana veritabanı | **PostgreSQL** |
| İlişki grafı | **Neo4j** |
| İş kuyruğu | **Celery + Redis** |
| Nesne deposu | **MinIO** |
| Frontend | **Next.js + Tailwind** |
| Grafik | **Vega-Lite** |
| Çalıştırma | **Docker Compose** |

[§12]

## 3. Veri yerleşimi

- **PostgreSQL** ana veritabanıdır: seriler, değerler (çekilme zamanı ve
  revizyonlarla), elle girilen göstergeler, haberler, grafikler, onaylar,
  çekme işleri, sürümlü prompt'lar. [§4, §5, §9]
- **Neo4j** ilişkiler ve test kayıtları için **asıl kaynaktır;** ilişki bilgisi
  iki yerde tutulmaz. İlişki kendi düğümüdür (2+ seriye ve kaynak haberlere
  bağlı); her test ayrı kayıttır. [§9]
- **MinIO:** TÜİK/TCMB ham cevapları ve LLM çağrılarının ham kaydı (istek +
  cevap, anahtarlar maskeli). [§12]
- Graf **Neo4j Browser**'dan izlenir; admin panelinde graf görünümü yok. [§9]

## 4. Zamanlama ve işler

- RSS taraması **15 dakikada bir** çalışır. [§6]
- Veri çekme işleri kodla izlenir: `istendi → çekiliyor → tamamlandı / hata`.
  İşler ve ajanlar "yaşıyorum" sinyali verir; sinyal kesilen iş/haber takılmış
  sayılır. Sabit süre sınırı yoktur. [§4.4, §4.6]
- Veri bekleyen fikir/görsel **park edilir**, veri gelince kaldığı yerden
  sürer. [§4.4]
- Yarıda kalan haber otomatik yeniden denenmez; başarısız haberler listesine
  düşer. [§4.6]

## 5. LLM katmanı

- Dört ajan: **DeepSeek v4.1 Flash**, sağlayıcı **EVREN**, yedek
  **OpenRouter.** [§4]
- **Jev** (etiketleme ve katalog araması puanlaması): önce TypeSafe, kredi
  bitince OpenRouter. [§7]
- Embedding yok; ölçüm yetersiz çıkarsa OpenRouter'daki
  `openai/text-embedding-3-large` eklenir. [§7]
- Eski projeden kurallar: araç turlarında JSON şeması yalnız son turda;
  sızan şablon belirteçleri temizlenir; 429'da uzun ve artan bekleme. [§13]
- Her haberin LLM maliyeti kaydedilir; bütçe sınırı yok. [§4.5]

## 6. Grafik üretimi

- Görselleştirme ajanı Vega-Lite tarifini yazar; veriyi kod bağlar ve çizer.
  [§4.3, §10]
- Grafik **yayın anında** en güncel veriyle çizilir; aynı tarif sitedeki
  interaktif grafiği ve sosyal medya PNG'sini üretir. [§10]

## 7. Admin kimlik doğrulama

Tek admin; kullanıcı adı + şifre (şifre `.env`'de hash olarak), oturum
çerezi. [§11]
