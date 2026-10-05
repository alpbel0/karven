# Arama ölçümü (Task 2.6)

- tarih: 2026-10-05 07:09 UTC
- ilk koşular, liste düzeltmesinden önce: 80%
- liste düzeltmesiyle sonucu değişen sorgu: yok
- sorgu / koşu: 40 / 42
- ortalama süre: 10.8 sn

## Özet

- **İsabet oranı:** 80% (28/37 olumlu koşu), eşik 70%: **EŞİĞİ GEÇTİ**
- Haber sorguları: 70% (14/20), ağaç sorguları: 82% (14/17)
- Sonuçlar: {'hit': 28, 'miss': 5, 'candidate_only': 4, 'correct_empty': 5}
- İsabetli sonucun ortalama sırası: 1.11
- Olumsuz sorgularda yanlış pozitif: 0/5 (0%); isabet oranına dahil değildir
- Başarısız arama (`search_failed`, ıskalama sayılmaz): 0
- Tekrarlı koşulan sorgu: 1; koşular arasında sonucu değişen: yok

## Sorgu sorgu

| id | grup | istek | sonuç | sıra | strong |
|---|---|---|---|---|---|
| h01 | haber | Eylül ayı yıllık TÜFE enflasyonu | hit | 1 | DF_TUFE_SDMX_2003; DF_TUFE_SDMX_TT10; DF_TUFE_SDMX_TT01 |
| h02 | haber | Aylık tüketici fiyat endeksi değişimi | hit | 1 | DF_TUFE_SDMX_2003; DF_TUFE_SDMX_TT10; DF_TUFE_SDMX_TT03 |
| h03 | haber | Piyasa katılımcıları anketine göre yıl sonu yıllık TÜFE beklentisi | hit | 1 | bie_pkauo; bie_urbek |
| h04 | haber | Yıl sonu TCMB politika faiz oranı beklentisi | hit | 1 | bie_pkauo |
| h05 | haber | Dolar kuru, ABD doları döviz alış | hit | 1 | bie_dkdovytl |
| h06 | haber | Euro döviz alış kuru | hit | 1 | bie_dkdovytl |
| h07 | haber | Merkez Bankası toplam uluslararası rezervleri | hit | 1 | bie_abres2; bie_abreserv; bie_ulusdovlkd |
| h08 | haber | Aylık konut satış sayısı | hit | 1 | DF_SATIS_SEKLI_SATIS_DURUMU_V3; DF_SATIS_SEKLI_SATIS_DURUMU_V3; DF_MEVSIM_TAKVIM_V3 |
| h09 | haber | Kira artışı, yeni kiracı kira endeksi | miss | - | - |
| h10 | haber | Konut fiyat endeksi, Türkiye geneli | hit | 1 | bie_kfe |
| h11 | haber | İmalat sanayi kapasite kullanım oranı | hit | 1 | bie_kko2; bie_kkoisma |
| h12 | haber | Türkiye'nin brüt dış borç stoku | hit | 1 | bie_brutdbborclu; bie_brutdbsdds; bie_brutdbalctcmb |
| h13 | haber | Merkezi yönetim bütçe dengesi | miss | - | DF_GENEL_DEVLET_ACIK_FAZLA_BORC_V2 |
| h14 | haber | Toplam ihracat tutarı, dolar cinsinden | hit | 1 | TUIK_BI_GTS; TUIK_BI_OTS; TUIK_BI_GTS |
| h15 | haber | Devlet tahvili ihracı | miss | - | - |
| h16 | haber | Havayolu ile giren yabancı ziyaretçi sayısı | hit | 1 | TUIK_TURIZM_SINIR_MILLIYET; TUIK_TURIZM_SINIR_KAPI; TUIK_TURIZM_SINIR_VATANDAS |
| h17 | haber | Kişi başına elektrik tüketimi | hit | 1 | CIP_ENR-GK054-O0015 |
| h18 | haber | Açık piyasa repo ve ters repo işlemleri | candidate_only/candidate_only/candidate_only | -/-/- | bie_abmbbil1; bie_abanlbil |
| a01 | agac | Aylık işsizlik oranı | hit | 2 | DF_ISGUCU_AYLIK_TEMEL_ISGUCU_C; DF_ISGUCU_AYLIK_TAMAMLAYICI_GOSTERGE_C; DF_ISGUCU_AYLIK_TAMAMLAYICI_GOSTERGE_C |
| a02 | agac | Sanayi üretim endeksi | hit | 1 | DF_SANAYI_URETIM_ENDEKS_ANA_C; DF_SANAYI_URETIM_ENDEKS_ANA_C; DF_SANAYI_URETIM_ENDEKS_ALT_C |
| a03 | agac | Ekonomik güven endeksi | hit | 1 | DF_EKONOMIK_GUVEN_SDMX_C |
| a04 | agac | Tüketici güven endeksi | hit | 1 | DF_TUKETICI_EGILIM_SDMX |
| a05 | agac | İl bazında kişi başına gayrisafi yurt içi hasıla | hit | 3 | CIP_UHS-GK096-O0011; CIP_UHS-GK095-O0011; DF_UH_BH_KISI_BASI |
| a06 | agac | İllere göre toplam doğurganlık hızı | hit | 1 | CIP_DGM-GK378140487-O40205 |
| a07 | agac | İllere göre genel doğurganlık hızı | miss | - | CIP_DGM-GK378140487-O40205 |
| a08 | agac | Trafiğe kaydı yapılan motorlu kara taşıtı sayısı | hit | 1 | DF_MOTORLU_KARA_TASIT_KAYDI_YAPILAN_IBBS_V2; DF_TRAFIK_KAZA_KAYITLI_ARAC_OLU_YARALI_V1; DF_MOTORLU_KARA_TASIT_KAYDI_V1 |
| a09 | agac | Müze ve ören yerlerini ziyaret eden kişi sayısı | hit | 1 | DF_MUZE_ORENYER_ZYARETCI |
| a10 | agac | Belediyelerin atık yönetimi istatistikleri, il bazında | candidate_only | - | CIP_CVRBA-GK1697132-O40508 |
| a11 | agac | Öğrenci başına eğitim harcaması, Türk lirası | hit | 1 | DF_EGITIM_HARCAMA_EGITIM_DUZEY_OGRENCI_BASI |
| a12 | agac | Buğday üretici fiyatı | hit | 1 | DF_TARIM_URUNLERI_UFE_MADDE_FIYAT_V2; DF_TARIM_URUNLERI_GRUP_MADDE_ENDEKS_TAORBA_V2; DF_TARIM_URUNLERI_GRUP_MADDE_ENDEKS_TAORBA_V2 |
| a13 | agac | İhracat birim değer endeksi | hit | 1 | DF_IHRACAT_BIRIM_DEGER_V1 |
| a14 | agac | Mevsim ve takvim etkisinden arındırılmış ihracat miktar endeksi | hit | 1 | DF_TAKVIM_ETKISINDEN_ARINDIRILMIS_IHRACAT_V2; DF_TAKVIM_ETKISINDEN_ARINDIRILMIS_IHRACAT_V2 |
| a15 | agac | TÜFE bazlı reel efektif döviz kuru | hit | 1 | bie_rktufey |
| a16 | agac | Cari yıl sonu cari işlemler dengesi beklentisi, medyan | hit | 1 | bie_urbeka; bie_pkauo |
| a17 | agac | Reel efektif döviz kuru, birim iş gücü maliyeti bazlı | miss | - | - |
| o01 | olumsuz | Gram altın fiyatı | correct_empty | - | - |
| o02 | olumsuz | Emekli maaşı tutarı | correct_empty | - | - |
| o03 | olumsuz | Spot piyasada elektrik fiyatı | correct_empty | - | - |
| o04 | olumsuz | Avrupa doğalgaz depolarında doluluk oranı | correct_empty | - | - |
| o05 | olumsuz | Bitcoin fiyatı | correct_empty | - | - |

## Iskalar ve yanlış pozitifler (ayrıntı)

### h09: Kira artışı, yeni kiracı kira endeksi (miss)
- beklenen: bie_ykke, bie_bk
- strong: []
- aday: []

### h13: Merkezi yönetim bütçe dengesi (miss)
- beklenen: TURCAT_MALI{'INDICATOR': '61'}, TURCAT_MALI{'INDICATOR': '69'}
- strong: ['DF_GENEL_DEVLET_ACIK_FAZLA_BORC_V2:UH_DH_DAFBD.MTRY.1_1.14.A']
- aday: []

### h15: Devlet tahvili ihracı (miss)
- beklenen: TURCAT_MALI{'INDICATOR': '129'}, TURCAT_MALI{'INDICATOR': '83'}
- strong: []
- aday: ['bie_pydibs:TP.TRB100227T13', 'TURCAT_MALI:50', 'TURCAT_MALI:38']

### h18: Açık piyasa repo ve ters repo işlemleri (candidate_only)
- beklenen: bie_pyrepo, bie_rtrnr
- strong: ['bie_abmbbil1:TP.AB.K042', 'bie_abanlbil:TP.AB.A01']
- aday: ['bie_pyrepo:TP.API.REP.TL.G1']

### a07: İllere göre genel doğurganlık hızı (miss)
- beklenen: DF_DOGUM_GDH_C{'INDICATOR': 'NG_GDH'}
- strong: ['CIP_DGM-GK378140487-O40205:3.TR1']
- aday: ['DF_DOGUM_KDH_C:TR1.NG_KDH', 'DF_DOGUM_IL_TDH_C:TR1.NG_TDH']

### a10: Belediyelerin atık yönetimi istatistikleri, il bazında (candidate_only)
- beklenen: DF_ATIK_BELEDIYE_ATIKYONETIMI_V1, DF_ATIK_BELEDIYE_HIZMET_V1, DF_ATIK_BELEDIYE_HIZMET_YONETIM_GOSTERGE_V1
- strong: ['CIP_CVRBA-GK1697132-O40508:3.TR1']
- aday: ['DF_ATIK_BELEDIYE_ATIKYONETIMI_V1:6._T.TR1.TN', 'DF_ATIK_BELEDIYE_HIZMET_V1:6.TR1.TN']

### a17: Reel efektif döviz kuru, birim iş gücü maliyeti bazlı (miss)
- beklenen: bie_rkbigm{'SERIE': 'TP.RK.BIGM'}
- strong: []
- aday: []

### h18: Açık piyasa repo ve ters repo işlemleri (candidate_only)
- beklenen: bie_pyrepo, bie_rtrnr
- strong: ['bie_abmbbil1:TP.AB.K042']
- aday: ['bie_abanlbil:TP.AB.A01', 'bie_rtrnr:TP.RP03.MKR']

### h18: Açık piyasa repo ve ters repo işlemleri (candidate_only)
- beklenen: bie_pyrepo, bie_rtrnr
- strong: ['bie_abmbbil1:TP.AB.K042']
- aday: ['bie_abanlbil:TP.AB.A01', 'bie_rtrnr:TP.RP03.MKR']

