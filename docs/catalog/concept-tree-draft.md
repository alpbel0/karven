# Kavram ağacı — taslak v1 (Task 3.5.1)

> **ESKİ.** Yerini `concept-tree.yaml` (v2.1, 2026-10-04) aldı; kurallar DECISIONS §7'de.
> Bu dosya yalnız tarihsel kayıt içindir.

Kaynaklar: TCMB EVDS katalog kategorileri (54.663 seri, `classification_path`
üst düzeyleri), TÜİK tema başlıkları ve DSD veri yapıları (435 seri), 27 Eylül
2026 Jev etiketleme testinin gösterdiği boşluklar (sektör kırılımları, anket
cevap dağılımları, devlet tahvilleri, bölgesel seriler).

**Kurallar**

- Kavram **konudur**, ölçüm biçimi değil. "Enflasyon beklentisi" ayrı bir dal
  değil: konu `fiyatlar › tüketici fiyatları`, nitelik `beklenti/anket`
  (ayrı etiket alanı). Aynı kural "yıllık / aylık değişim" için de geçerli
  (ölçüm türü alanı).
- Coğrafya (Türkiye / il / bölge / ülke), sektör (NACE/ISIC faaliyet kolu),
  para birimi ve vade de **ayrı etiket alanlarıdır**; ağaçta tekrar edilmez.
  Böylece "Şanlıurfa konut satışları" = `konut › konut satışları` +
  coğrafya `il: Şanlıurfa`.
- Arşiv serileri aynı kavrama bağlanır (`arşiv` ayrı bir bayrak).
- Bir seri tek kavrama bağlanır; gerçekten iki konuya ait seriler için ikinci
  kavram isteğe bağlıdır.

## Üst seviye (onay istenen kısım) — 18 dal

| # | Üst kavram | Kapsadığı TCMB/TÜİK başlıklarından örnekler |
|---|---|---|
| 1 | **Fiyatlar ve enflasyon** | TÜFE, ÜFE, hizmet ÜFE, yurt dışı ÜFE, tarımsal fiyatlar, geçinme endeksleri, sektörel enflasyon beklentileri |
| 2 | **Döviz kurları** | TCMB döviz kurları, reel efektif kur, efektif kurlar |
| 3 | **Faiz ve para politikası** | Politika faizi, reeskont/avans, piyasa faizleri, mevduat/kredi faizleri, azami faizler |
| 4 | **Para, kredi ve bankacılık** | Para arzı, mevduat, krediler, TCMB bilançosu/haftalık vaziyet, zorunlu karşılıklar, banknot, kart ve ödeme sistemleri, banka dışı finansal kuruluşlar |
| 5 | **Finansal piyasalar ve menkul kıymetler** | Borsa, DİBS ve tahviller, özel sektör borçlanma senetleri, yurt dışı yerleşik portföyü, altın ve kıymetli madenler (piyasa) |
| 6 | **Ödemeler dengesi ve dış finansman** | Cari işlemler, doğrudan yatırımlar, portföy, rezervler, uluslararası yatırım pozisyonu, dış borç (brüt/kısa vadeli), özel sektör dış kredileri |
| 7 | **Dış ticaret** | İhracat/ithalat, dış ticaret endeksleri, mal grupları, dış ticaret nakliye, hizmet ticareti |
| 8 | **Ulusal hesaplar ve büyüme** | GSYH (üretim/harcama/gelir yöntemi, dönemsel/yıllık, bölgesel), verimlilik, bileşik öncü göstergeler |
| 9 | **Sanayi, üretim ve iş dünyası** | Sanayi üretimi, kapasite kullanımı, ciro endeksleri, iktisadi yönelim anketi, girişimcilik ve iş kayıtları, sektör riski |
| 10 | **İşgücü, istihdam ve ücretler** | İşsizlik, istihdam, işgücüne katılım, ücretler ve asgari ücret, çalışan sayısı |
| 11 | **Kamu maliyesi** | Bütçe, vergi, kamu finansmanı, iç/dış borç stoku, özelleştirme gelirleri |
| 12 | **Konut, inşaat ve gayrimenkul** | Konut fiyat endeksi, konut/iş yeri satışları, yapı izinleri, inşaat maliyetleri, ticari gayrimenkul fiyatları |
| 13 | **Güven ve eğilim endeksleri** | Tüketici güveni, reel kesim güveni, sektörel güven, ekonomik güven (anket cevap dağılımları nitelik alanında) |
| 14 | **Hanehalkı ve firmalar: bilanço, gelir, tüketim** | Hanehalkı gelir-tüketim-yoksulluk, finansal hesaplar (konsolide/akım/stok), firma bilançoları ve gelir tabloları, döviz pozisyonu |
| 15 | **Enerji, emtia ve tarım** | Brent/petrol, doğalgaz, elektrik, emtia fiyatları, tarımsal üretim |
| 16 | **Ulaştırma, turizm ve iletişim** | Motorlu taşıtlar, trafik, turizm/seyahat, bilişim teknolojileri kullanımı |
| 17 | **Nüfus ve sosyal** | Nüfus (ADNKS), doğum, evlenme/boşanma, eğitim, sağlık, göç, çevre ve atık |
| 18 | **Uluslararası karşılaştırma** | IMF, OECD, diğer merkez bankaları, satın alma gücü paritesi, diğer ülke verileri |

## İkinci seviye (bilgi için; onaydan sonra genişletilecek)

1. **Fiyatlar ve enflasyon** — tüketici fiyatları (manşet, çekirdek, ana
   harcama grupları, özel kapsamlı göstergeler) · üretici fiyatları (yurt içi,
   yurt dışı, hizmet) · tarımsal fiyatlar (girdi, üretici) · geçinme ve bölgesel
   fiyatlar · ithalat/ihracat birim değerleri
2. **Döviz kurları** — nominal kurlar (TCMB alış/satış/efektif, piyasa) ·
   çapraz kurlar · reel efektif kur · kur sepetleri
3. **Faiz ve para politikası** — politika ve kısa vade faizleri · mevduat
   faizleri · kredi faizleri · devlet borçlanma faizleri · reeskont/avans ·
   azami faizler
4. **Para, kredi ve bankacılık** — parasal büyüklükler · mevduat ·
   krediler (tüketici, ticari, konut, taşıt, kart) · merkez bankası bilançosu ·
   banka bilançoları · ödeme sistemleri ve kartlar · banka dışı finansal
   kuruluşlar · kredi eğilim anketi
5. **Finansal piyasalar ve menkul kıymetler** — hisse senedi · devlet iç
   borçlanma senetleri · özel sektör borçlanma senetleri · yurt dışı
   yerleşik portföyü · altın ve kıymetli madenler · sürdürülebilir (ESG)
   borçlanma senetleri
6. **Ödemeler dengesi ve dış finansman** — cari işlemler · doğrudan
   yatırımlar (akım, stok) · portföy yatırımları · rezervler · uluslararası
   yatırım pozisyonu · dış borç stoku · kısa vadeli dış borç · özel sektör dış
   kredileri
7. **Dış ticaret** — ihracat · ithalat · dış ticaret dengesi · birim değer ve
   miktar endeksleri · ürün/mal grupları · ülke grupları · hizmet ticareti ·
   nakliye araçları
8. **Ulusal hesaplar ve büyüme** — GSYH (üretim, harcama, gelir) ·
   bölgesel GSYH · nihai tüketim · yatırım (sabit sermaye) · verimlilik ·
   öncü göstergeler
9. **Sanayi, üretim ve iş dünyası** — sanayi üretimi · kapasite kullanımı ·
   ciro ve hizmet endeksleri · iktisadi yönelim (sipariş, üretim, istihdam
   beklentileri) · girişimcilik ve iş demografisi · sektör riski
10. **İşgücü, istihdam ve ücretler** — işsizlik · istihdam · işgücüne katılım ·
    ücretler ve asgari ücret · kayıtlı çalışan sayısı · emeklilik/sosyal
    güvenlik ödemeleri
11. **Kamu maliyesi** — bütçe gelir/gider/denge · vergi gelirleri · kamu
    borç stoku (iç, dış) · kamu finansmanı · özelleştirme
12. **Konut, inşaat ve gayrimenkul** — konut fiyatları · konut satışları ·
    yapı izinleri ve iskân · inşaat maliyetleri · ticari gayrimenkul · kira
13. **Güven ve eğilim endeksleri** — tüketici güveni · reel kesim güveni ·
    sektörel güven (hizmet, perakende, inşaat) · ekonomik güven
14. **Hanehalkı ve firmalar** — hanehalkı gelir ve yoksulluk · hanehalkı
    tüketim · finansal varlık/yükümlülük hesapları · firma bilançoları ve gelir
    tabloları · firmaların döviz varlık/yükümlülükleri · iştirakler
15. **Enerji, emtia ve tarım** — petrol ve akaryakıt · doğalgaz · elektrik ·
    metaller ve diğer emtia · tarımsal üretim ve hayvancılık
16. **Ulaştırma, turizm ve iletişim** — motorlu taşıtlar · trafik ·
    turizm gelir/gider ve ziyaretçi · seyahat · bilişim teknolojileri
17. **Nüfus ve sosyal** — nüfus · doğum ve ölüm · evlenme/boşanma · göç ·
    eğitim · sağlık · çevre, su ve atık
18. **Uluslararası karşılaştırma** — IMF verileri · OECD verileri · diğer
    merkez bankaları · satın alma gücü paritesi · ülke makro göstergeleri

## Diğer etiket alanları (kapalı listeler)

- **Ölçüm türü:** seviye/tutar · fiyat/kur · endeks · aylık % değişim · yıllık
  % değişim · yılbaşından % değişim · 12 aylık ortalama % değişim · oran/pay ·
  olasılık/cevap dağılımı · denge/yayılma değeri · sayı/adet
- **Veri niteliği:** gerçekleşen · beklenti/anket · tahmin/projeksiyon
- **Coğrafya:** Türkiye · bölge (İBBS-1/2) · il · ülke/ülke grubu · dünya
- **Sektör:** yok · NACE/ISIC faaliyet kolu (kod + ad) · kurumsal sektör
  (hanehalkı, finansal olmayan şirketler, genel yönetim, finansal kuruluşlar,
  yurt dışı)
- **Sıklık:** günlük · haftalık · aylık · çeyreklik · altı aylık · yıllık
- **Kurum:** TCMB · TÜİK · HMB · BDDK · SPK · İTO · diğer (kaynak adıyla)
- **Ek nitelikler:** para birimi · vade · alış/satış · mevsim etkisinden
  arındırılmış mı · reel/nominal · arşiv
