# Faz 1 — Veri katmanı

## Faz amacı

Resmî veriyi (TÜİK ve TCMB) ve haberleri sisteme almak. Ortak veri modeli
kurulur; her değerin **ne zaman çekildiği** en baştan doğru kaydedilir.
Çekirdek seriler 2000'den itibaren önceden yüklenir, diğer seriler talep
üzerine çekilir. Talep üzerine çekmeyi veri çekme ajanı ile kod birlikte
yürütür: ajan planlar ve hataları yorumlar, kod izler ve bekletir. Haberler
5 RSS kaynağından tam metinleriyle alınır.

## Task 1.1 — Ortak veri modeli

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 0.3

**Referanslar:** `docs/DECISIONS.md` §5, §13

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Kurum, seri (meta bilgisiyle: ad, kurum kategorisi/tablosu, birim,
      frekans, kırılım, kapsadığı yıllar) ve gözlem (dönem + değer) tablolarını kur.
- [ ] Her gözlem için **çekilme zamanı** zorunlu alan olsun.
- [ ] **Revizyonlar saklanır:** bir dönemin değeri düzeltilirse yeni kayıt
      olarak eklenir, eskisi silinmez; okuyan taraf son değeri kullanır.
- [ ] Çekme işi durum tablosu: `istendi → çekiliyor → tamamlandı / hata`,
      "yaşıyorum" sinyali zamanı, hata nedeni (Task 1.5 kullanır).

**Kabul kriteri:** Migration boş veritabanında kuruluyor; çekilme zamanı
olmayan bir gözlem veritabanına yazılamıyor.

## Task 1.2 — TÜİK bağlayıcısı

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.1

**Referanslar:** `docs/source-profiles/tuik.md`, `docs/DECISIONS.md` §5;
bilgi için `docs/source-profiles/reference/connector-data-coverage-research.md`

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] TÜİK'in seri listesini (katalog için meta bilgi) çek.
- [ ] Bir seriyi 2000-01-01'den itibaren çek; kaynak profilindeki formatları
      (SDMX, JSON-stat vb.) destekle.
- [ ] Her yazılan gözleme çekilme zamanını kaydet.
- [ ] TÜİK'ten gelen ham cevabı MinIO'da sakla.
- [ ] Hataları anlaşılır biçimde döndür (veri çekme ajanı teşhis için kullanır).

**Kabul kriteri:** Gerçek TÜİK'ten en az bir aylık ve bir yıllık seri
2000'den itibaren eksiksiz çekiliyor; seri listesi meta bilgisiyle kaydediliyor.

## Task 1.3 — TCMB bağlayıcısı

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.1

**Referanslar:** `docs/source-profiles/tcmb.md`, `docs/DECISIONS.md` §5;
bilgi için `docs/source-profiles/reference/tcmb-connector-completion-implementation-plan.md`,
`docs/source-profiles/reference/tcmb-vintage-timestamps.md`, `docs/source-profiles/reference/connector-data-coverage-research.md`

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] TCMB EVDS seri listesini (katalog için meta bilgi) çek.
- [ ] Bir seriyi 2000-01-01'den itibaren çek; frekans bilgisini doğru kaydet.
- [ ] Her yazılan gözleme çekilme zamanını kaydet.
- [ ] TCMB'den gelen ham cevabı MinIO'da sakla.
- [ ] Hataları anlaşılır biçimde döndür.

**Kabul kriteri:** Gerçek EVDS'ten en az bir günlük ve bir aylık seri 2000'den
itibaren eksiksiz çekiliyor; seri listesi meta bilgisiyle kaydediliyor.

## Task 1.4 — Çekirdek seriler

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.2, Task 1.3

**Referanslar:** `docs/DECISIONS.md` §5; bilgi için
`docs/source-profiles/reference/tcmb-core-backfill-plan.md`

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Çekirdek seri listesini kullanıcıyla belirle (**proje içinde karar**,
      `docs/DECISIONS.md` → Proje içinde verilecek kararlar #1).
- [ ] Çekirdek serileri 2000-01-01'den itibaren yükle.
- [ ] Çekirdek serileri düzenli güncelleyen zamanlanmış görevi kur.
- [ ] Talep üzerine çekilen seriler otomatik olarak çekirdeğe **alınmaz**.
- [ ] **Veri kesilme uyarısı:** bir kaynaktan veri sessizce gelmemeye başlarsa
      ya da cevabın biçimi değişirse uyarı kaydı oluşur (admin panelinde
      görünür, Task 5.5). Hangi durumda uyarı verileceği kullanıcıyla
      belirlenir (**proje içinde karar**, `docs/DECISIONS.md` → Proje içinde
      verilecek kararlar #7).

**Kabul kriteri:** Kararlaştırılan çekirdek listenin tamamı yüklü ve güncelleme
görevi yeni dönemi kendiliğinden ekliyor.

## Task 1.5 — Talep üzerine çekme (kod tarafı)

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.2, Task 1.3

**Referanslar:** `docs/DECISIONS.md` §4.4

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Bir seri için çekme talebi oluşturma; aynı seri için ikinci talep yeni iş
      açmaz, mevcut işe bağlanır.
- [ ] Durum geçişleri kodla yapılır: `istendi → çekiliyor → tamamlandı / hata`.
- [ ] Çekme işi "yaşıyorum" sinyali verir; sinyal geldikçe beklenir (sabit
      20 dakika sınırı yok), sinyal kesilirse iş takılmış sayılır.
- [ ] Talep eden iş (fikir ya da görsel) **park edilir**; çekme tamamlanınca
      kaldığı yerden devam ettirilir.
- [ ] Hata olursa veri çekme ajanı devreye girer (Task 1.6).

**Kabul kriteri:** Uzun süren bir çekme (20 dakikadan uzun) sinyal verdiği
sürece tamamlanıyor; sinyali kesilen iş takılmış olarak işaretleniyor; park
edilen iş veri gelince devam ediyor.

## Task 1.6 — Veri çekme ajanı

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.5, Task 0.5

**Referanslar:** `docs/DECISIONS.md` §4.4

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Ajan (DeepSeek v4.1 Flash) talebi çekme planına çevirir: hangi kurum,
      hangi tablo/seri, hangi parametre.
- [ ] Hata olursa teşhis koyar: yeniden dener, alternatif önerir ya da "bu veri
      bu kaynakta yok" der.
- [ ] Çekilemeyen her veri için kayıt: hangi veri, neden, ajanın önerisi
      (admin listesi Task 5.3'te gösterilir).
- [ ] İzleme, durum değişikliği ve bekleme **ajanın işi değildir** (kod yapar).
- [ ] Birinci ajan ve graph ajanı bu ajana doğrudan soru sorabilir (ör. "haber
      Eylül verisi diyor, bende Ağustos var").

**Kabul kriteri:** Gerçek bir talep plan haline gelip çekiliyor; bilinçli
olarak bozulmuş bir talep teşhis edilip çekilemeyen veriler kaydına düşüyor.

## Task 1.7 — Haber kaynakları: RSS + tam metin

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.1

**Referanslar:** `docs/DECISIONS.md` §6

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] 5 RSS kaynağını oku: Sabah ekonomi, Habertürk ekonomi, Sözcü ekonomi,
      BloombergHT, CNN Türk finans.
- [ ] Her haberin sayfasından **tam metni** çek; RSS özeti yetmez.
- [ ] Haberi başlık, link, yayın zamanı ve tam metinle kaydet.
- [ ] Haberler **15 dakikada bir** çekilir.
- [ ] Tekrar haber kontrolü MVP'de **yok**; her haber işlenir.

**Kabul kriteri:** Beş kaynaktan gerçek haberler tam metinleriyle kaydediliyor;
tam metni çekilemeyen haber nedeniyle birlikte işaretleniyor.

## Task 1.8 — Elle girilen göstergeler

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.1

**Referanslar:** `docs/DECISIONS.md` §5

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] TÜİK/TCMB'de olmayan göstergeler (ör. asgari ücret) için elle
      güncellenen tablo.
- [ ] Hangi göstergelerin gireceği ve nasıl girileceği kullanıcıyla belirlenir
      (**proje içinde karar**, `docs/DECISIONS.md` → Proje içinde verilecek
      kararlar #6).
- [ ] Elle girilen değerler de diğer seriler gibi kaynak notu ve giriş
      zamanıyla saklanır; katalogda aranabilir.

**Kabul kriteri:** Kararlaştırılan göstergeler tabloda; katalog aramasında
bulunuyor ve grafikte kullanılabiliyor.
