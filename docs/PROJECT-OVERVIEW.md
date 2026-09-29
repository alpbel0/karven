# Karven — Proje Tanıtımı

> Projeyi ilk kez okuyan biri için kısa tanıtım. Bağlayıcı kararlar
> `docs/DECISIONS.md`'de, gereksinimler `docs/REQUIREMENTS.md`'de, teknik yapı
> `docs/PRODUCTION-ARCHITECTURE.md`'de, iş sırası `roadmap/ROADMAP.md`'dedir.
> Bu belge ile onlar çelişirse **`DECISIONS.md` geçerlidir.**
>
> Eski projenin uzun vizyon belgesi (`KARVEN-PROJE-DOKUMANI.md`) 2026-09-29'da
> yeniden başlamayla bu kısa tanıtıma dönüştü; eski hali git geçmişinde durur.

## 1. Karven nedir

Karven, Türkçe ekonomi haberlerini okuyup haberin arkasındaki ekonomik
ilişkileri resmî veriyle (TÜİK, TCMB) görünür hâle getiren ve sonuçları
herkese açık bir web sitesinde yayımlayan bir sistemdir. Bir chatbot değildir;
çıktısı, haberlere eşlik eden grafikler ve test edilmiş ilişkilerdir.

## 1b. Adın anlamı

**Karven** üç parçadan oluşur:

- **Kar:** Eski Asur döneminde Anadolu'daki ticaret merkezleri için kullanılan
  *kārum* kelimesinden. Bu merkezlerin en bilineni Kayseri yakınlarındaki
  Kültepe'dir (antik Kaniş); buradaki tüccarların kil tabletlere yazdığı
  ticari kayıtlar Anadolu'da yazıya geçmiş en eski ekonomik kayıtlar
  arasındadır.
- **Ve:** Türkçe *veri* kelimesinden.
- **N:** Söyleyişi tamamlayan ses; ayrı bir anlamı yoktur.

Fikir: Anadolu'da ekonominin ilk yazılı kayıtlarından bugünün resmî verisine.
Karven de ekonomiyi kayıtla, veriyle anlatır.

> Sitedeki "Hakkında" metninde kullanılacak tarihî ayrıntılar (tarihler,
> tablet sayısı vb.) yayından önce kaynağından doğrulanır.

## 2. Temel problem

Ekonomi haberleri çoğu zaman tekil olaylar olarak okunur. Bir gelişmenin
geçmiş verilerle ve başka göstergelerle ilişkisi aynı yerde, anlaşılır
biçimde gösterilmez. Karven'in amacı haberi özetlemek değil, haberin
arkasındaki ilişkileri resmî veriyle göstermektir.

## 3. Örnek

> "Benzine zam geldi."

- **Haber görseli:** aylık benzin fiyatı grafiği; haberde geçen rakam
  "haberde belirtilen" notuyla gösterilir (hüküm verilmez).
- **İlişki fikirleri:** ör. benzin fiyatı ile enflasyon ya da döviz kuru
  arasındaki ilişki. Knowledge graph ajanı önce veriyi görmeden hipotez yazar,
  kod resmî veriyle test eder, sonuç ilişki grafına yazılır.
- Grafikler çizilir, kodla kontrol edilir; admin onaylarsa haber sayfası
  sitede yayımlanır.

## 4. Akış

```
Haber (RSS, tam metin)
  → Birinci ajan ──(a) haber görselleri──────────────────────────┐
        │  (b) ilişki fikirleri                                  │
        ↓                                                        │
    Knowledge graph ajanı (hipotez → kodla test → grafa yaz)     │
        │                                                        │
        ↓                                                        ↓
  Birinci ajan son listeyi yapar → Görselleştirme ajanı → Kod kontrolü
  → Admin onayı → Site
Yan kol: gerektiğinde Veri çekme ajanı
```

## 5. Dört ajan

| Ajan | İşi |
|---|---|
| Birinci ajan | Haberi okur; haber görsellerini ve ilişki fikirlerini çıkarır; son listeyi seçer. |
| Knowledge graph ajanı | Fikir grafta yoksa hipotez yazar; kod test eder; sonucu grafa yazar. |
| Görselleştirme ajanı | Vega-Lite grafik tarifini yazar; kod kontrolünün geri bildirimiyle düzeltir. |
| Veri çekme ajanı | Eksik veriyi çekme planına çevirir, hatada teşhis koyar; izlemeyi kod yapar. |

Ajanlar hiçbir zaman sayısal değer görmez; test, çizim ve kontrol koddadır.

## 6. Temel ilkeler

- **Hesabı kod yapar, ajan yorumlar.** Ajan hipotez ve tarif yazar; sayılar,
  test ve çizim koddadır.
- **Önce hipotez, sonra veri.** İlişki hipotezi veri görülmeden kilitlenir.
- **Resmî kaynak ve iz.** Her değerin kaynağı, çekilme zamanı ve revizyonları
  saklanır; grafikte kaynak notu bulunur.
- **İnsan onayı.** MVP'de her yayın admin onayından geçer.
- **Küçük ama canlıda çalışan akış önce gelir;** yeni özellik, çekirdek akış
  canlıda kanıtlandıktan sonra eklenir.

## 7. MVP'de olmayanlar

Sayısal iddia doğrulama, tekrar haber kontrolü, CSV indirme, otomatik yayın ve
sosyal medya paylaşımı, admin panelinde graf görünümü. Ayrıntı:
`roadmap/post-mvp.md`.
