"""Uploaded files -> page images -> documents (page 1 + page 2).

A page 1 always carries the ZPIZ QR code (document ID + VK); page 2 never does.
So a page with a QR code starts a new document and the next page without one is its page 2.
"""
import io
import re
import zipfile
from dataclasses import dataclass, field

from PIL import Image, ImageSequence
from pyzbar.pyzbar import ZBarSymbol, decode

MAX_ZIP_BYTES = 500 * 1024 * 1024  # unpacked, per upload


@dataclass
class Page:
    name: str  # file name, plus "#n" for the n-th frame of a multi-page file
    image: Image.Image
    qr: dict | None = None


@dataclass
class Document:
    pages: list[Page] = field(default_factory=list)

    @property
    def qr(self) -> dict | None:
        return next((p.qr for p in self.pages if p.qr), None)


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def unpack(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Expands ZIPs and sorts everything by name (00000002 after 00000001, not after 00000010)."""
    out = []
    for name, data in files:
        if data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
                if sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
                    raise ValueError(f"{name}: ZIP unpacks to more than {MAX_ZIP_BYTES >> 20} MB")
                out += [(i.filename, z.read(i)) for i in infos]
        else:
            out.append((name, data))
    return sorted(out, key=lambda f: natural_key(f[0]))


def load_pages(name: str, data: bytes) -> list[Page]:
    """Every page of a TIFF (single or multi-page), PNG, JPEG or PDF, as 1-bit or greyscale images."""
    if data[:4] == b"%PDF":
        import fitz  # PyMuPDF, only needed for PDFs

        with fitz.open(stream=data, filetype="pdf") as pdf:
            images = [Image.open(io.BytesIO(p.get_pixmap(dpi=200, colorspace="gray").tobytes("png"))) for p in pdf]
    else:
        try:
            src = Image.open(io.BytesIO(data))
        except Exception as e:
            raise ValueError(f"{name}: not an image or PDF ({e})") from None
        # Fax-style scans stay 1-bit (8x less memory than greyscale on a 100-page batch).
        images = [f.convert("1" if f.mode == "1" else "L") for f in ImageSequence.Iterator(src)]
    if len(images) == 1:
        return [Page(name, images[0])]
    return [Page(f"{name}#{i + 1}", im) for i, im in enumerate(images)]


def read_qr(image: Image.Image) -> dict | None:
    """The ZPIZ QR code: 'ID: 140000001\\nVK: 123456789\\n...'. None if there is none."""
    for sym in decode(image, symbols=[ZBarSymbol.QRCODE]):
        text = sym.data.decode("utf-8", "replace")
        doc_id = re.search(r"^ID:\s*(\d+)", text, re.M)
        vk = re.search(r"^VK:\s*(\d+)", text, re.M)
        if doc_id:
            return {"document_id": doc_id.group(1), "vk": vk.group(1) if vk else None}
    return None


def group(pages: list[Page]) -> list[Document]:
    """A QR page starts a document. Without QR codes (bad scan) it falls back to pairs: odd page 1, even page 2."""
    docs: list[Document] = []
    for p in pages:
        p.qr = read_qr(p.image)
        if p.qr or not docs or len(docs[-1].pages) >= 2:
            docs.append(Document())
        docs[-1].pages.append(p)
    return docs
