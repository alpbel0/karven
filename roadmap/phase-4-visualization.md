# Faz 4 — Görselleştirme

## Faz amacı

Birinci ajanın son listesindeki haber görselleri ve ilişkiler için grafik
üretmek. Görselleştirme ajanı sayılara dokunmadan bir Vega-Lite tarifi yazar;
kod veriyi çekip çizer ve grafiği kontrol eder. Grafik yayın anında en güncel
veriyle çizilir; aynı tarif hem sitedeki interaktif grafiği hem sosyal medya
PNG'sini üretir.

## Task 4.1 — Grafik tarif formatı

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.1

**Referanslar:** `docs/DECISIONS.md` §10

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Vega-Lite tabanlı tarif formatı: seriler, dönem, dönüşüm, başlık, eksenler.
- [ ] Grafik türleri: çizgi, endeksli çizgi (çift eksen yok), gecikmeli dağılım,
      çubuk.
- [ ] "Haberde belirtilen" notu: haberdeki rakam ve dönemi grafik üzerinde not
      olarak gösterilir; resmî veriyle karşılaştırılmaz.
- [ ] Tarifte değer bulunmaz; değerleri kod çizim anında ekler.

**Kabul kriteri:** Her grafik türü için örnek tarif, gerçek veriyle çizilebiliyor.

## Task 4.2 — Görselleştirme ajanı

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 4.1, Task 3.5

**Referanslar:** `docs/DECISIONS.md` §4.3, §10

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Son listedeki her öğe için tarif yazar; sayılara dokunmaz.
- [ ] Dönemi kapalı listeden seçer: son 2 yıl, son 5 yıl, 2017'den beri,
      2000'den beri (okuyucu sitede değiştirebilir).
- [ ] Seviye / değişim seçer: haber görselinde habere uyan; ilişki grafiğinde
      testte kullanılan dönüşüm.
- [ ] Haber başına en fazla **5 haber görseli + 5 ilişki grafiği**; fazlası
      silinmez, saklanır.
- [ ] Kod kontrolünden dönen geri bildirimle düzeltir.

**Kabul kriteri:** Gerçek bir haberin son listesi için geçerli tarifler
üretiliyor; sınırlar aşılmıyor.

## Task 4.3 — Kodla grafik kontrolü

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 4.1

**Referanslar:** `docs/DECISIONS.md` §10

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Kontroller kodla yapılır (ajan yok): eksen etiketleri, birimler, dönem,
      çizilen değerlerin veriyle birebir aynı olması, kaynak notunun varlığı.
- [ ] Başarısız grafik geri bildirimle görselleştirme ajanına döner; **en fazla
      2 tur**.
- [ ] Hâlâ başarısızsa yayımlanmaz, admin incelemesi için saklanır.

**Kabul kriteri:** Bilerek bozulmuş bir tarif (yanlış birim, eksik kaynak notu)
yakalanıyor; iki tur sonra düzelmeyen grafik admin'e saklanıyor.

## Task 4.4 — Çizim: yayın anında güncel veri, site ve PNG

**Repo:** `karven`
**Alan:** `backend`, `frontend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 4.1, Task 4.3

**Referanslar:** `docs/DECISIONS.md` §10, §11

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Grafik **yayın anında** en güncel veriyle çizilir (admin onayından sonra).
- [ ] Kaynak notunda verinin son dönemi yazar (ör. "Veri: TÜİK, Eylül 2026'ya kadar").
- [ ] Aynı tariften sitede interaktif grafik ve sosyal medya için **PNG**.
- [ ] Her grafik için tarif, kullanılan veri sürümleri ve kaynak notu saklanır.

**Kabul kriteri:** Onaydan önce yeni bir dönem eklendiğinde yayımlanan grafik o
dönemi içeriyor; PNG ile interaktif grafik aynı veriyi gösteriyor.
