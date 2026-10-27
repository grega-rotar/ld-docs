# Navodila za referente: pregled obrazcev LD

Storitev prebere skene obrazcev »Izjava o višini tuje pokojnine« (LD): kljukice, številke v glavi, QR kodo in
lastnoročno napisano valuto, državo in znesek. Kar ni povsem jasno, označi z rumeno. Vi pregledate označena
polja, jih potrdite ali popravite in izvozite paket za ZPIZ.

Naslov strani vam da skrbnik sistema (IT).

## 1. Prijava

1. Odprite stran v brskalniku.
2. V polje **Ključ za dostop** vpišite ključ, ki ste ga dobili od skrbnika, in kliknite **Prijava**.
3. Dokler niste prijavljeni, je polje za nalaganje sivo in piše »Najprej se prijavite s ključem.«

Če piše **»Ključ ni pravilen. Nov ključ dobite pri skrbniku sistema.«**, preverite, da ste ključ prepisali v
celoti (brez presledkov), sicer se obrnite na skrbnika. Ključa ne pošiljajte po e-pošti in ga ne delite.

**Odjava** (desno zgoraj) pozabi ključ v tem brskalniku.

## 2. Nalaganje skenov

Povlecite datoteke na polje **Povlecite skene sem** ali kliknite nanj in jih izberite. Sprejme:

- **TIFF, PDF, PNG, JPG**: posamezne strani ali datoteke z več stranmi;
- **ZIP** s temi datotekami: cel paket naenkrat.

Strani se razvrstijo po imenu (`00000001.tif`, `00000002.tif` …) in razdelijo na dokumente po QR kodi na
strani 1. Če stran 2 manjka, se zaradi tega ne zamaknejo drugi dokumenti.

Med branjem levi seznam (vrsta) kaže, kaj se z vsakim dokumentom dogaja: *v vrsti*, *bere stran 1*,
*bere rokopis*. Zgoraj piše, koliko dokumentov je prebranih in približno koliko časa je še potrebnega.
Dokumente, ki so že prebrani, lahko pregledujete, medtem ko se drugi še berejo.

Nov paket naložite z gumbom **Nove datoteke**.

## 3. Pregled dokumenta

Na sredini je obrazec s prebranimi vrednostmi, desno sken (**Stran 1** / **Stran 2**; klik poveča sliko).
Ko kliknete v označeno polje, se sken desno pomakne na stran, na kateri je to polje.

### Kaj pomeni rumeno

Rumeno polje je **za preverbo**: storitev ni dovolj prepričana, da je vrednost prav prebrana
(zanesljivost pod 90 %). Pri polju piše zanesljivost (npr. *zanesljivost 70 %*) in pogosto razlog, npr.
»Decimalki sta zapisani kot dve odprti zanki: prebrano 00, lahko tudi 02.«

Polja brez rumene so prebrana zanesljivo. Številki **ID dokumenta** in **VK** sta prebrani iz QR kode in sta
vedno točni.

### Pravilno ali Shrani popravek

Pri vsakem rumenem polju primerjajte vrednost s skenom (desno se odpre stran, na kateri je polje):

- Vrednost je prava: kliknite **Pravilno**.
- Vrednost ni prava: v polje vpišite pravo vrednost in kliknite **Shrani popravek** (pri »da/ne« izberite
  pravo možnost). Že pregledano polje znova odprete z gumbom **Spremeni**. Popravljena vrednost je zapisana modro, prebrana prečrtano poleg nje. Popravek se
  shrani takoj in ostane tudi, če stran osvežite.

**Znesek** vpišite tako, kot je na obrazcu, s piko za tisočice in vejico za decimalke: `2.500,02`.
Če vpišete kaj, kar ni število (npr. črko O namesto ničle), se pokaže »Znesek zapišite s številkami,
npr. 2.500,02.« in nič se ne shrani.

### Potrdi dokument in naprej

Ko so vsa polja pregledana, kliknite **Potrdi dokument in naprej →** (ali pritisnite **Enter**, ko kazalec
ni v polju za vnos). Dokument dobi oznako **Potrjeno**, števec zgoraj (npr. *38 od 51 pregledanih*) se
poveča in odpre se naslednji dokument. Rumena polja, ki jih niste posebej potrdili ali popravili, se ob tem
štejejo kot potrjena.

Tudi dokument brez rumenih polj potrdite: s tem zapišete, da ste ga pregledali.

## 4. Filtri

Nad seznamom dokumentov:

- **Vsi**: ves paket;
- **Za preverbo**: samo dokumenti z rumenimi polji, ki še niso potrjeni;
- **Potrjeni**: že potrjeni dokumenti.

Število ob filtru pove, koliko dokumentov je v njem. Če filter nima zadetkov, kliknite **Pokaži vse**.

## 5. Tipkovnica

| Tipka | |
|---|---|
| **↑** / **↓** | prejšnji / naslednji dokument |
| **Enter** | potrdi dokument in pojdi na naslednjega (ko kazalec ni v polju za vnos) |

## 6. Izvoz

Spodaj levo (**Izvoz**) in na koncu, ko je ves paket pregledan:

- **JSON za ZPIZ**: podatki v obliki LDDokumentP1 + LDDokumentP2, s popravki. Pri vsakem popravljenem polju
  je zapisano, kaj je bilo prebrano. To datoteko uvozite v sistem ZPIZ.
- **CSV**: en dokument v vrstici, za Excel (podpičje, decimalna vejica). Stolpci povedo, katera polja so
  bila popravljena in katera so bila za preverbo.

Izvozite lahko kadar koli. Če kak dokument še ni pregledan, piše npr. »13 dokumentov še ni pregledanih« in ti
dokumenti so v izvozu označeni kot nepregledani.

Ko so pregledani vsi dokumenti, se pokaže povzetek (npr. »Vseh 51 dokumentov je pregledanih«, koliko jih je
brez sprememb, koliko polj je popravljenih) in izbira izvoza.

## 7. Napake in kaj storiti

| Kaj vidite | Kaj storiti |
|---|---|
| **Bralnik ne deluje** v glavi (rdeča pika) in obvestilo »Bralnik ni dosegljiv …« | Dokumenti počakajo v vrsti. Čez nekaj minut kliknite **Poskusi znova**. Če traja dlje, obvestite skrbnika sistema. |
| Dokument z rdečo oznako, npr. **Ni strani 2** | Preberite sporočilo v okvirju. Manjkajočo stran skenirajte in naložite znova (cel dokument: stran 1 in 2). |
| Datoteka z oznako **Ni sken** (npr. `porocilo.docx`) | To ni sken. Naložite TIFF, PDF, PNG, JPG ali ZIP. |
| Dokument je ostal v napaki zaradi bralnika | Ko bralnik spet deluje, dokument naložite znova. |
| »Ključ ni pravilen« | Glejte točko 1. |
| Popravek se ne shrani (rdeče besedilo pod poljem) | Preverite obliko (znesek: `2.500,02`) in poskusite znova. |
| Stran se ne odziva ali kaže drugačno napako | Osvežite stran (F5); prebrani paket in popravki ostanejo. Če se ponavlja, obvestite skrbnika in mu povejte čas in številko zadeve. |

Številke zadeve lahko pošljete skrbniku, skenov in izvozov pa ne pošiljajte po e-pošti: vsebujejo osebne
podatke.

## 8. Koliko časa ostanejo rezultati

Prebrani paketi in vaši popravki ostanejo na strežniku **7 dni**, nato se izbrišejo, ker vsebujejo osebne
podatke. Paket izvozite in uvozite v sistem ZPIZ, preden poteče. Skeni se na strežniku ne shranjujejo.
