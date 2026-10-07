# Faz 3 — İlişki akışı

## Faz amacı

Projenin çekirdeğini kurmak: bir haber birinci ajana girer; birinci ajan
haberde anlatılanları (haber görselleri) ve haberden çıkan ekonomik ilişkileri
çıkarır; ilişkiler knowledge graph ajanında önce hipotez olarak yazılır, sonra
kodla test edilir ve Neo4j grafına kaydedilir. Graf başta boştur ve her haberle
büyür. Ajanlar hiçbir zaman değer görmez; test tamamen koddur.

## Task 3.1 — İlişki grafı (Neo4j)

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Tamamlandı
**Bağımlılıklar:** Task 0.3, Task 1.1

**Referanslar:** `docs/DECISIONS.md` §9

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [x] Düğümler: seri, **ilişki** (kendi düğümü), test kaydı, haber.
- [x] İlişki: 2+ seriye bağlı; mekanizma, beklenen yön, gecikme aralığı, durum.
- [x] Her test ayrı kayıt; üzerine yazılmaz.
- [x] İlişki kaynak haberlere bağlanır (bir ilişkiye çok haber).
- [x] Reddedilen ilişkiler silinmez.
- [x] İlişkiler ve test kayıtları için **asıl kaynak Neo4j**; PostgreSQL'de
      ilişki bilgisinin kopyası tutulmaz (seriler, haberler vb. Postgres'te).

**Kabul kriteri:** Bir ilişki, iki test kaydı ve iki kaynak haber yazılıp Neo4j
Browser'da görülebiliyor; aynı ilişki ikinci kez yazılınca kopya oluşmuyor.

## Task 3.2 — Sade test motoru

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.1

**Referanslar:** `docs/DECISIONS.md` §8

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] İki seri: korelasyon + gecikme araması (hipotezdeki gecikme aralığında).
- [ ] 3+ seri: regresyon; her açıklayıcının gecikmesi kendi ikili gecikme
      aramasından gelir; en az gözlem = frekans eşiği × açıklayıcı sayısı (ör. 2
      açıklayıcılı aylıkta 72).
- [ ] Eldeki **tüm veri** kullanılır; haber tarihinden bağımsız.
- [ ] Test ham seviyede yapılmaz; hipotezdeki dönüşüm uygulanır: yıllık %
      değişim, dönemsel % değişim ya da fark.
- [ ] Frekans eşleştirme: düşük frekansa inilir; fiyat/oran/endeks → dönem
      ortalaması, akım → dönem toplamı (tür katalogdan gelir).
- [ ] Hipotezde nominal TL tutarı işaretli seriler TÜFE ile reel hale getirilir.
- [ ] İki dönem: **bütün yıllar** ve **2017–2026**.
- [ ] En az gözlem: aylık 36, üç aylık 12, yıllık 10; altında "veri yetersiz".
- [ ] Sonuç: destekleniyor / desteklenmiyor / veri yetersiz; beklenen yön ters
      çıkarsa desteklenmiyor.
- [ ] İki dönemin sonucu ayrı saklanır; ilişki durumu: ikisi de destekliyorsa
      destekleniyor, ikisi de desteklemiyorsa desteklenmiyor, farklıysa
      **dönem sonuçları farklı**.

**Kabul kriteri:** Bilinen sonuçlu yapay veriyle birim testleri geçiyor
(güçlü ilişki → destekleniyor, gürültü → desteklenmiyor, kısa seri → veri
yetersiz); gerçek iki seriyle test uçtan uca çalışıyor.

## Task 3.3 — Birinci ajan

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 2.4, Task 2.5, Task 1.6, Task 1.7

**Referanslar:** `docs/DECISIONS.md` §2, §4.1

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Haberin tam metnini okur; "ekonomik değil" diye haber elenmez.
- [ ] **(a) Haber görselleri** listesi: haberde anlatılan seriler; haberde geçen
      rakam ve dönemi ile birlikte.
- [ ] **(b) İlişki fikirleri** listesi.
- [ ] Sınırlar: en fazla 8 ilişki fikri, 8 görsel adayı, 60 araç çağrısı.
- [ ] Araçlar: kavram ağacı, katalog arama, veri durumu, graf okuma, graph
      ajanına sorma, veri çekme ajanıyla konuşma.
- [ ] Değer görmez.
- [ ] Haber yeni bir dönemden söz ediyor ama veri yoksa veri çekme ajanına sorar.
- [ ] Graph ajanının cevaplarından sonra görselleştirilecek son listeyi yapar.

**Kabul kriteri:** Gerçek bir haberde (ör. benzin zammı) doğru haber görseli
(benzin fiyatı serisi) ve en az bir anlamlı ilişki fikri çıkıyor; sınırlar
aşılmıyor; ajana hiçbir değer gitmiyor.

## Task 3.4 — Knowledge graph ajanı

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 3.1, Task 3.2, Task 2.4

**Referanslar:** `docs/DECISIONS.md` §4.2, §9

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Fikir gelince önce grafa bakar; ilişki varsa var olan sonucu kullanır,
      haberi ilişkiye bağlar, yeniden test etmez.
- [ ] "Aynı ilişki" = aynı seri kümesi + aynı roller (kim etkiliyor, kim
      etkileniyor); gecikme, dönüşüm ve mekanizma metni farkı yeni ilişki açmaz.
- [ ] Aynı ilişki yeniden gelince son sonuç "veri yetersiz" ya da son test
      30 günden eskiyse güncel veriyle kodla yeniden test edilir (aynı hipotez,
      yeni test kaydı).
- [ ] Yoksa **önce hipotez**: mekanizma, beklenen yön, gecikme aralığı, seriler
      (2+), dönüşüm ve nominal TL işaretleri — veriyi görmeden, kilitlenir.
- [ ] Testi kod yapar (Task 3.2); sonuç grafa yazılır.
- [ ] Birinci ajana sonuç ve gerekçe döner.
- [ ] Veri eksikse veri çekme ajanına sorar; fikir park edilir, veri gelince
      devam eder.

**Kabul kriteri:** Grafta olmayan bir fikir hipotez → test → graf yoluyla
kaydediliyor; aynı fikir ikinci kez gelince yeniden test edilmeden var olan
sonuç dönüyor; veri eksik fikir park edilip veri gelince tamamlanıyor.

## Task 3.5 — Haber akışının orkestrasyonu

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 3.3, Task 3.4

**Referanslar:** `docs/DECISIONS.md` §3

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Yeni haber → birinci ajan → (b) için graph ajanı → birinci ajanın son
      listesi → görselleştirmeye aktarım (Faz 4).
- [ ] (a) listesi graph ajanına gitmez, doğrudan görselleştirmeye aktarılır.
- [ ] Park edilen işler veri gelince kaldığı yerden devam eder.
- [ ] Bir haberin işlenme durumu izlenebilir (hangi adımda, bekliyor mu, bitti mi).
- [ ] Hatalar (`docs/DECISIONS.md` §4.6): hatalı fikir tek başına düşer; yarıda
      kalan haber otomatik denenmez, başarısız haberler listesine düşer; süre
      sınırı yok; ajanlar "yaşıyorum" sinyali verir, ne veri bekleyen ne sinyal
      veren haber takılmış sayılır.
- [ ] Her haberin LLM maliyeti kaydedilir.

**Kabul kriteri:** Gerçek bir haber, veri eksikliği olan bir fikir dahil, uçtan
uca işleniyor ve görselleştirmeye hazır son liste oluşuyor.
