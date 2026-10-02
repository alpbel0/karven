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
**Durum:** Tamamlandı (2026-09-30, canlıda doğrulandı)
**Bağımlılıklar:** Task 0.3

**Referanslar:** `docs/DECISIONS.md` §5, §13

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [x] Kurum, seri (meta bilgisiyle: ad, kurum kategorisi/tablosu, birim,
      frekans, kırılım, kapsadığı yıllar) ve gözlem (dönem + değer) tablolarını kur.
- [x] Her gözlem için **çekilme zamanı** zorunlu alan olsun.
- [x] **Revizyonlar saklanır:** bir dönemin değeri düzeltilirse yeni kayıt
      olarak eklenir, eskisi silinmez; okuyan taraf son değeri kullanır.
- [x] Çekme işi durum tablosu: `istendi → çekiliyor → tamamlandı / hata`,
      "yaşıyorum" sinyali zamanı, hata nedeni (Task 1.5 kullanır).

**Kabul kriteri:** Migration boş veritabanında kuruluyor; çekilme zamanı
olmayan bir gözlem veritabanına yazılamıyor.

## Task 1.2 — TÜİK bağlayıcısı (üst task)

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.1

**Referanslar:** `docs/source-profiles/tuik.md`, `docs/DECISIONS.md` §5;
bilgi için `docs/source-profiles/reference/connector-data-coverage-research.md`
ve araştırma klasörü `Desktop/KARVEN-ARAŞTIRMA/tuik/`

**Kapsam kararı (kullanıcı, 2026-09-30):** Bir kaynak bağlanırken **bütün
kanallarıyla** bağlanır. TÜİK'in 12 kanalı aşağıdaki alt tasklara bölündü ve
sırayla yapılır. Kapsam dışı yalnızca Mikro Veri Setleri (resmî kurumsal
başvuru gerektirir).

**Kapsam kararı güncellemesi (kullanıcı, 2026-10-01):** TÜİK bu kuralla
tamamlanır (1.2h dahil). Sonraki kaynaklarda (Task 1.3 TCMB'den itibaren)
araştırma geniş kalır, ama koda yalnız gereken kanallar alınır; diğerleri
gerektiğinde eklenir. Bağlanan kanalda katalog eksiksiz, veri talep üzerine
(`docs/DECISIONS.md` §5).

**Ortak kurallar (bütün alt tasklar):**
- Kaynaktan bağımsız ortak bağlayıcı arayüzü (1.2a'da tanımlanır): seri
  listesi, seri çekme, ortak hata türleri (`not_found`, `empty`, `timeout`,
  `format_changed`, `source_error`).
- Aynı veri seti + aynı boyutlar **tek seri**dir; hangi kanaldan geldiği
  gözlem kaydında tutulur. Kanallar birbirinin yedeğidir.
- Baz yılı seri kimliğinin parçasıdır (baz değişince yeni seri doğar).
- Her gözleme çekilme zamanı; ham cevap MinIO'da
  `sources/tuik/YYYY/MM/DD/<veri-seti>/<id>.<uzantı>`.
- İstek başına 30 sn zaman aşımı, 3 tekrar deneme (artan bekleme). Hız,
  2026-09-30 ölçümüne göre: databrowser2'de **2 paralel istek, sabit bekleme
  yok** (%100 başarı, ~1000 istek/dk); 3+ paralelde TÜİK HTTP 200 ile
  "Yönlendiriliyor..." HTML sayfası döndürüyor → bu sayfa yavaşlatma sinyali
  sayılır: bekle, tekrar dene, paralelliği 1'e düşür. Değerler ayarda.
- Seri olmayan içerik (bülten metni, yayın, sınıflama) için kaynaktan
  bağımsız yeni tablolar ilgili alt taskta kullanıcıyla tasarlanır.

**Kabul kriteri (üst task):** Bütün alt tasklar canlıda doğrulanmış olarak
tamamlandı.

### Task 1.2a — databrowser2 + ortak bağlayıcı arayüzü + seri kataloğu

**Durum:** Tamamlandı (2026-09-30, canlıda doğrulandı) · **Bağımlılıklar:** Task 1.1

- [x] Ortak bağlayıcı arayüzü ve ortak hata türleri.
- [x] Veri seti listesi ve doğru sürüm numaraları (databrowser2'nin kendi
      kataloğu `/api/core/nodes/1/catalog`, token gerekmiyor; 467 veri seti).
- [x] Katalog **veri seti + boyut listeleri** olarak yazılır (DECISIONS §7,
      2026-09-30): her veri setinin adı, kategorisi, frekansı, yıl aralığı,
      toplam gözlem sayısı ve her boyutun tam kod listesi (varsa hiyerarşi).
      Seri satırları önceden yazılmaz; değerler yazılmaz.
- [x] Kaynağın bildirdiğinden az gözlem veren veri setleri "kaynak eksik
      bildiriyor" notuyla işaretlenir; filtreli istekte hata veren veri
      setleri için filtresiz isteğe yedek yol.
- [x] Bir seriyi 2000-01-01'den itibaren çekme: SDMX-CSV birincil, JSON-stat
      yedek (biri ayrıştırılamazsa diğeri; biçim değişikliği tespit edilir).
- [x] Bölge kodu `TR` geçersiz olan veri setlerinde geçerli kodları keşfetme.

**Kabul kriteri:** Canlı container'da: bütün TÜİK veri setleri boyut
listeleriyle katalogda; TÜFE (aylık) ve GSYH (yıllık) seri olarak oluşturulup
2000'den itibaren kaynakta ne varsa eksiksiz çekiliyor.

### Task 1.2b — nsiws SDMX (resmî servis)

**Durum:** Tamamlandı (2026-09-30, canlıda doğrulandı) · **Bağımlılıklar:** Task 1.2a · **Anahtar:** `TUIK_API_KEY`

- [x] API anahtarını Keycloak'ta kısa ömürlü token'a çevirme ve yenileme.
- [x] Veri çekme; databrowser2 başarısız olursa yedek kanal olarak devreye girer.

**Kabul kriteri:** Aynı seri nsiws'ten çekiliyor ve databrowser2 ile aynı
değerleri veriyor; yedeğe geçiş canlıda görüldü.

### Task 1.2c — Turcat (IMF SDDS)

**Durum:** Devam ediyor (TÜİK kaynaklı satırlar canlıda eşleştirildi;
"yeni dönem" kontrolü Task 1.4'te bağlanacak) · **Bağımlılıklar:** Task 1.2a

- [x] Kilit göstergelerin son + önceki değerleri.
- [ ] "Yeni dönem yayımlandı mı" kontrolü olarak kullanım (Task 1.4 kesilme
      uyarısına girdi).

**Kabul kriteri:** Canlıda Turcat göstergeleri okunuyor ve katalogdaki
serilerle eşleşiyor.

**Not (2026-09-30):** Turcat'ın 258 satırının çoğu TCMB/Hazine verisi; yalnız
~25'i TÜİK. 7 TÜİK göstergesi (TÜFE, ÜFE, sanayi üretimi, istihdam, işsizlik,
ücret endeksi, nüfus) değer karşılaştırmasıyla elle eşleştirildi
(`catalog_links`, method=manual). Turcat istihdam/işsizlikte ilk yayım
değerini gösteriyor; güncel tabloya bağlandı (kullanıcı kararı). GSYH (13
satır) databrowser2'de çeyreklik ulusal olmadığı için Task 1.3'te EVDS'e,
TCMB/Hazine satırları Task 1.3 sonrasına kaldı. Dış ticaret (2 satır)
2026-10-01'de Genel Ticaret toplamlarına bağlandı (ölçek ×1e6).

### Task 1.2d — MEDAS + CİP (bölgesel veri)

**Durum:** Tamamlandı (2026-09-30, canlıda doğrulandı) · **Bağımlılıklar:** Task 1.2a

- [x] İl/ilçe düzeyi göstergeler (`duzey` 1-4); kırılım `breakdown` alanında.

**Kabul kriteri:** Bir bölgesel gösterge 81 il için canlıda çekiliyor.

**Not (2026-09-30):** Kaynaktan bağımsız `region_crosswalk` tablosu
(migration 0006): CİP plaka kodu ↔ İBBS düzey-3, 81/81 (2'si CİP yazım
hatası nedeniyle elle). Sadece iller (kullanıcı kararı; ilçeler gerektiğinde).
79 CİP göstergesinin 48'i databrowser2 serilerine 81 ilin tamamında değer
karşılaştırmasıyla elle bağlandı. Kalan 30'unun (yapı izinleri, il işgücü
oranları, eğitim/sağlık oranları, sinema, tarım, göç, ölüm hızı, yaşam süresi
vb.) databrowser2'de il düzeyinde karşılığı yok; CİP tek il kaynağı.
`CIP_ses123` kaynağın kendisinde HTTP 500 veriyor.

### Task 1.2e — Sınıflama Sunucusu

**Durum:** Tamamlandı (2026-09-30, canlıda doğrulandı) · **Bağımlılıklar:** Task 1.2a

- [x] Sınıflama hiyerarşileri (NACE, COICOP, İBBS …) için kaynaktan bağımsız
      tablo (kullanıcıyla tasarlanır) ve çekme.

**Kabul kriteri:** En az COICOP ve İBBS hiyerarşisi canlıda eksiksiz yüklü.

**Not (2026-09-30):** Sınıflama Sunucusu'nun tamamı yüklendi (kullanıcı
kararı): 133 sürüm, 1.231.655 kalem (kaynaktaki 124 satır birebir tekrar),
15 eşleşme tablosu (38.394 satır). Tablolar kaynaktan bağımsız (migration
0008-0010); kalem kimliği kod + üst kod + ad (aynı kod birden çok üst başlık
altında geçebiliyor). Yükleme sürüm sürüm yazılıyor (bellek ~300 MB). 2
paralel istek, 120 sn zaman aşımı. databrowser2 boyutları normalize edilmiş
kod + İngilizce ad uyumu (≥%80) ile sınıflamalara bağlandı: 248 bağ (İBBS,
NACE/ISIC, SITC, CPA, COICOP). TÜFE'nin TÜİK'e özel 7 haneli madde kodları ve
bazı ÜFE ürün boyutları kısmi uyum nedeniyle bağlanmadı (Faz 2'de ele
alınabilir). Aylık yenileme Task 1.5'te zamanlanacak.

### Task 1.2f — ZK uygulamaları: turizmapp, Seçim Dağıtım, Biruni Yayın Sistemi

**Durum:** Tamamlandı (2026-10-02, canlıda doğrulandı) · **Bağımlılıklar:** Task 1.2a

- [x] turizmapp 3 konu.
      2026-10-01: ortak eski-ZK motoru (`zk.py`, düz HTTP, oturum başına ≥2 sn
      bekleme; kural oturum başına, IP başına değil). Bütün form yolları
      katalogda (10 veri seti); gelir/gider kategorileri raporlardan keşfedilip
      kataloğa eklendi (`discovered_from_report`). Sınır giriş-çıkış manşetleri
      yüklü (1977/1996–2025, aylık); gelir/gider yalnız eski anket (2003–2012),
      güncel turizm geliri TCMB ödemeler dengesinden (Task 1.3). Kural (kullanıcı,
      2026-10-01): katalog eksiksiz doldurulur, veri ihtiyaç anında çekilir.
- [x] Seçim Dağıtım 10 tablonun tamamı.
      2026-10-02: 10 tablo katalogda (her tablonun gerçekten sunduğu seçimler,
      seçim çevresi/ilçe/bölge/ülke/gümrük listeleri); parti ve aday adları
      raporlardan keşfedildi (her tablo × her seçim için bir küçük rapor).
      Dönem = seçim tarihi (`irregular` sıklık, migration 0012); tarih seçilen
      seçimden alınır, asla tahmin edilmez (hatalı 94 kayıt yedeklenip
      silindi, kullanıcı onayıyla). Sandık/mahalle düzeyi kodlar yalnız rapor
      istendiğinde eklenir.
- [x] Yayın Sistemi kataloğu (seri olmayan içerik tablosu kullanıcıyla tasarlanır).
      2026-10-01: kaynaktan bağımsız `documents` tablosu (migration 0011), 634
      yayın canlıda; yalnız katalog + bağlantı (dosyalar sonra, kullanıcı kararı).

**Kabul kriteri:** Üç uygulamadan da canlıda veri alınıyor.

**Not (2026-10-01, keşif):** Üç uygulama da tarayıcısız, düz HTTP (eski ZK AU
protokolü: `dtid` + `cmd.0/uuid.0/data.0`) ile çalışıyor. TÜİK'in önündeki
koruma çok hızlı ardışık istekleri sessizce bekletiyor (0,2 sn arayla asılı
kalıyor, 2 sn arayla geçiyor) → adımlar arasında bekleme zorunlu. Turizm ve
seçim birer rapor sihirbazı: kombinasyonlar katalogda gezilir, manşet
raporları önceden, geri kalanı ilk istendiğinde üretilir (kullanıcı kararı).
Seçimde önceki 9 başarısız tablo, sunucunun doldurduğu listeler ve zorunlu
"mutlak/oransal" seçimi yüzündendi; Python'dan çalıştı.

### Task 1.2g — bi.tuik (Qlik) dış ticaret

**Durum:** Tamamlandı (2026-10-01, canlıda doğrulandı) · **Bağımlılıklar:** Task 1.2a

- [x] Qlik protokolüyle dış ticaret raporları (4 ana kategori).

**Kabul kriteri:** En az bir dış ticaret tablosu canlıda çekiliyor.

**Not (2026-10-01):** Sihirbaz yerine Qlik motorunun kendi protokolü
(anonim oturum + CSRF + WebSocket JSON-RPC) tarayıcısız kullanılıyor
(kullanıcı kararı). İki veri seti: `TUIK_BI_GTS` (Genel Ticaret, 2013-,
164,6 M satırlık olgu tablosu) ve `TUIK_BI_OTS` (Özel Ticaret, 1996-);
bütün boyutlar `_T` toplam koduyla, ölçü boyutu USD/EUR/TRY/QTY1/QTY2. Her
kombinasyon ilk istendiğinde çekilir; manşetler (akış, ülke, fasıl, BEC, il;
USD) önceden yüklendi: 1.769 seri, ~389 bin gözlem. HS kodları GTİP gibi
sıfır dolgulu saklanır (motor dolgusuz verir). Olmayan bir kod seçimi
`not_found` verir (motor sessizce filtresiz toplam döndürüyordu). Bağlar:
HS → bütün GTİP yılları (kapsama 1,0), ISIC → ISIC Rev.4, SITC → SITC Rev.4
(yalnız kod; çeviriler farklı); BEC ad uyumu düşük, bağlanmadı. Paralel
oturum hızlandırmıyor → tek oturum. `MEVSIMSEL_*` ve `ILFAALIYET_*` alanları
anlamı kanıtlanamadığı için alınmadı.

### Task 1.2h — WAF kanalları: Veri Portalı toplu indirme + basın bültenleri

**Durum:** Başlamadı · **Bağımlılıklar:** Task 1.2a

- [ ] Tarayıcı otomasyonu: önce headless Playwright + gizleme; geçmezse
      sanal ekranlı gerçek Chrome için ayrı Docker servisi.
- [ ] Toplu indirme kataloğu (`updatedAt` dahil) ve veri seti dosyaları.
- [ ] Basın bülteni metinleri (seri olmayan içerik tablosu kullanıcıyla tasarlanır).
- [ ] Excel/PDF çıktıları.

**Kabul kriteri:** Canlıda WAF aşılarak katalog, bir veri seti dosyası ve bir
bülten metni alınıyor.

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

**Not (2026-09-30, kullanıcı kararı):** TÜİK katalog yenilemesi her veri
setinin boyut listesini verinin tam varsayılan görünümünden yokluyor (büyük
setlerde 10-30 MB, toplam çalıştırma ~40 dk). Küçültülmüş yoklama canlıda
gizli boyutu düşürdüğü için (ÜFE ürün boyutu) kullanılamaz. Zamanlanmış
yenilemede boyut listesi yalnızca veri setinin yapısı (structure) değiştiğinde
yeniden yoklanacak.

**Not (2026-09-30, kullanıcı kararı):** Kabul edilmiş `catalog_links`
eşleştirmeleri zamanlanmış bir işle düzenli olarak yeniden kontrol edilecek
(iki kaynak hâlâ aynı değeri veriyor mu); tutmayanlar uyarı olarak raporlanır.
Sınıflama Sunucusu (Task 1.2e) ayda bir yeniden yüklenir ve ardından boyut
bağları (`siniflama-link-dimensions`) yeniden hesaplanır.

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
