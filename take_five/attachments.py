"""
take_five/attachments.py

PDF attachment detection and extraction pipeline -- channel-agnostic.
Mirrors take_five/images.py's shape: channel adapters extract a
PDFAttachment from their webhook payload (with .fetch already bound to
that channel's auth/download mechanics), one core function
(handle_pdf_attachment) does the extraction work, and the calling webhook
code handles the DB write + pipeline firing -- the same division of
responsibility images.py's process_image() already uses for the DOCUMENT
classification branch.

Uses PyMuPDF (import name: fitz) rather than pdfplumber + a separate
page-rendering library. PyMuPDF does both text extraction and page-to-
image rendering in one pure-wheel dependency with no system binary
(poppler/ImageMagick) required -- pdfplumber's own page.to_image() needs
one of those, a real deployment complication on Render's standard Python
buildpack. See requirements.txt.

Only the email adapter (extract_email_file) is implemented so far --
GroupMe and SMS adapters follow the same PDFAttachment shape once their
channel-specific download mechanics (GroupMe's authenticated
file.groupme.com endpoint; Twilio's Basic-Auth MediaUrl) are wired in.
Mirrors images.py's own extract_whatsapp_image, which raises
NotImplementedError the same way in the meantime.
"""

import base64
import json as _json
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx
import pymupdf as fitz  # "import fitz" is PyMuPDF's deprecated alias -- see chat history

from take_five.images import ANTHROPIC_CLIENT
from take_five.models import VISION_MODEL

logger = logging.getLogger(__name__)

# A page below this many extracted characters is treated as "no usable
# native text" and gets the vision fallback -- protects against pages that
# technically have a stray character or two of embedded text (a page
# number, a watermark) but are otherwise a scanned image.
MIN_CHARS_PER_PAGE = 20

# Page render resolution for the vision fallback. 150 DPI balances
# legibility (small print, e.g. a slide deck's footnotes) against payload
# size / vision cost -- standard for scanned-document OCR.
RENDER_DPI = 150

# Plain-transcription prompt for the vision fallback -- deliberately NOT
# images.py's VISION_PROMPT, which classifies a casually-shared photo into
# MEDICATION/DOCUMENT/OTHER. A rendered PDF page is already known to be a
# document; it just needs its text transcribed, not triaged. Shares the
# same Anthropic client/model as images.py (import above), not the same
# system prompt.
PAGE_OCR_PROMPT = (
    "Transcribe all visible text from this image exactly as it appears, "
    "preserving line breaks and structure where meaningful (headings, "
    "bullet points, table rows). Output only the transcribed text -- no "
    "commentary, no markdown formatting, no preamble."
)


@dataclass
class PDFAttachment:
    fetch: Callable[[], Awaitable[bytes]]
    filename: Optional[str]
    channel: str  # "groupme" | "sms" | "email"


@dataclass
class PDFExtractionResult:
    text: str
    pages_total: int
    pages_vision_fallback: int
    success: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Channel adapters
# ---------------------------------------------------------------------------

def extract_email_file(form) -> Optional[PDFAttachment]:
    """
    form: the starlette FormData from request.form() in
    sendgrid_email.handle_inbound_email(). SendGrid's Inbound Parse sends
    one multipart field per attachment ("attachment1", "attachment2", ...),
    plus an "attachments" count field and an "attachment-info" JSON map
    keyed by those same field names with filename/type metadata -- see
    https://docs.sendgrid.com/for-developers/parsing-email/setting-up-the-inbound-parse-webhook

    Field format confirmed from SendGrid's own docs, not yet exercised
    against a real inbound email with an attachment -- verify against a
    live test send before relying on this in production, same caution as
    this module's existing SENDGRID_INBOUND_PUBLIC_KEY TODO.

    Only the FIRST pdf-typed attachment is picked up.
    """
    count = int(form.get("attachments", 0) or 0)
    if count == 0:
        return None

    info_raw = form.get("attachment-info")
    info = {}
    if info_raw:
        try:
            info = _json.loads(info_raw)
        except _json.JSONDecodeError:
            logger.warning("[attachments] Malformed attachment-info JSON from SendGrid")

    for i in range(1, count + 1):
        field_name = f"attachment{i}"
        upload = form.get(field_name)
        if upload is None:
            continue
        meta = info.get(field_name, {})
        content_type = (meta.get("type") or getattr(upload, "content_type", "") or "")
        if "pdf" not in content_type.lower():
            continue

        filename = meta.get("filename") or getattr(upload, "filename", None)

        async def _fetch(upload=upload) -> bytes:
            return await upload.read()

        logger.info(f"[attachments] PDF attachment found in inbound email: {filename}")
        return PDFAttachment(fetch=_fetch, filename=filename, channel="email")

    return None


async def _fetch_groupme_file(group_id: str, file_id: str, access_token: str) -> bytes:
    url = f"https://file.groupme.com/v1/{group_id}/files/{file_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers={"X-Access-Token": access_token})
        response.raise_for_status()
        return response.content


def extract_groupme_file(payload: dict, access_token: str) -> Optional[PDFAttachment]:
    """
    payload: the GroupMe webhook payload (same dict handle_groupme_webhook
    already has). Looks for an attachment of type == "file" (not "image" --
    see images.py's extract_groupme_image, which only handles that type).
    Confirmed against real traffic (2026-09-23 curl test): a PDF shared in
    GroupMe arrives as {'file_id': ..., 'type': 'file'}, with NO filename
    anywhere in the webhook payload -- GroupMe's file service doesn't
    appear to surface one without a separate metadata call, which this
    doesn't attempt yet (worth checking whether the download response
    carries a Content-Disposition header, next time this is exercised for
    real). PDFAttachment.filename is left None for this channel;
    confirmation/failure messages fall back to generic wording ("a
    document") rather than a real filename until/unless that's added.

    GroupMe's "file" type covers any non-image attachment, not just PDFs --
    there's no content-type in the payload to pre-filter on. This
    optimistically attempts PDF extraction on every file attachment and
    lets extract_pdf_text's own fitz.open() failure handle a non-PDF
    cleanly (PDFExtractionResult.success=False), rather than trying to
    sniff the type ahead of time.

    access_token: resolved by the caller via
    take_five.integrations.groupme.resolve_groupme_token(admin_person_id)
    -- attachments.py has no DB/circle access of its own, same separation
    images.py keeps between fetch auth (channel-specific) and the actual
    vision/extraction work (channel-agnostic).
    """
    attachments = payload.get("attachments", [])
    file_att = next((a for a in attachments if a.get("type") == "file"), None)
    if not file_att:
        return None

    file_id = file_att.get("file_id")
    group_id = str(payload.get("group_id", ""))
    if not file_id or not group_id:
        return None

    async def _fetch() -> bytes:
        return await _fetch_groupme_file(group_id, file_id, access_token)

    logger.info(f"[attachments] PDF file attachment found in GroupMe message: file_id={file_id}")
    return PDFAttachment(fetch=_fetch, filename=None, channel="groupme")


def extract_sms_file(payload: dict) -> Optional[PDFAttachment]:
    """
    payload: dict with NumMedia, MediaUrl0, MediaContentType0 -- the same
    Twilio inbound-MMS fields already used by images.py's extract_sms_image
    (fetch_image_as_base64's SMS branch). Only checks the first media item
    (index 0), matching that existing single-media-item convention rather
    than introducing support for multiple attachments Twilio can in
    principle send.

    Unlike email/GroupMe, MMS attachment reliability for non-image files is
    a real carrier-level unknown -- some carriers strip or reject a PDF
    before it ever reaches Twilio, silently (see chat history's design
    discussion). This function only detects what actually arrives; it
    can't detect what a carrier dropped before that.
    """
    num_media = int(payload.get("NumMedia", 0) or 0)
    if num_media == 0:
        return None
    content_type = (payload.get("MediaContentType0") or "").lower()
    if "pdf" not in content_type:
        return None
    media_url = payload.get("MediaUrl0")
    if not media_url:
        return None

    async def _fetch() -> bytes:
        # Twilio media URLs require Basic Auth with the account SID/token --
        # same mechanism as fetch_twilio_media/analyze_image's SMS branch,
        # duplicated here (not imported from twilio.py) to avoid a circular
        # import, same reasoning as _fetch_groupme_file above.
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        if not account_sid or not auth_token:
            raise RuntimeError("TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set -- cannot fetch MMS media")
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(media_url, auth=(account_sid, auth_token))
            response.raise_for_status()
            return response.content

    logger.info(f"[attachments] PDF MMS attachment found: content_type={content_type}")
    return PDFAttachment(fetch=_fetch, filename=None, channel="sms")


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def _extract_page_via_vision(page: "fitz.Page") -> str:
    """
    Renders one PDF page to a PNG and transcribes it via Claude vision.
    Returns "" (never raises) on any failure -- a single bad page
    shouldn't take down extraction for the whole document; the caller's
    page loop just gets less text for that page.
    """
    try:
        pix = page.get_pixmap(dpi=RENDER_DPI)
        png_bytes = pix.tobytes("png")
        image_data = base64.standard_b64encode(png_bytes).decode("utf-8")

        response = ANTHROPIC_CLIENT.messages.create(
            model=VISION_MODEL,
            max_tokens=2048,
            system=PAGE_OCR_PROMPT,
            messages=[{
                "role": "user",
                "content": [{
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_data},
                }],
            }],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"[attachments] Vision fallback failed for page: {e}", exc_info=True)
        return ""


async def extract_pdf_text(pdf_bytes: bytes) -> PDFExtractionResult:
    """
    pdfplumber-equivalent extraction via PyMuPDF: native text per page,
    falling back to a rendered-page vision call for any page with too
    little extractable text (scanned pages, image-only slides).
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.error(f"[attachments] Failed to open PDF: {e}")
        return PDFExtractionResult(
            text="", pages_total=0, pages_vision_fallback=0,
            success=False, error=str(e),
        )

    pages_text = []
    vision_fallback_count = 0

    for page_num in range(len(doc)):
        page = doc[page_num]
        native_text = page.get_text().strip()
        if len(native_text) >= MIN_CHARS_PER_PAGE:
            pages_text.append(native_text)
        else:
            vision_fallback_count += 1
            fallback_text = await _extract_page_via_vision(page)
            pages_text.append(fallback_text)

    doc.close()

    full_text = "\n\n".join(t for t in pages_text if t)
    logger.info(
        f"[attachments] Extracted {len(full_text)} chars from {len(pages_text)} pages "
        f"({vision_fallback_count} via vision fallback)"
    )
    return PDFExtractionResult(
        text=full_text,
        pages_total=len(pages_text),
        pages_vision_fallback=vision_fallback_count,
        success=bool(full_text.strip()),
        error=None if full_text.strip() else "No extractable text found on any page",
    )


async def handle_pdf_attachment(attachment: PDFAttachment) -> Optional[PDFExtractionResult]:
    """
    attachment.fetch() -> extract_pdf_text(). Returns None only on total
    failure to even fetch the bytes (bad download, network error) -- the
    caller decides what that means for its channel (error reply on
    SMS/GroupMe, log-and-continue on email). A fetch that succeeds but
    yields no extractable text is a PDFExtractionResult with success=False,
    not a None -- callers can distinguish "couldn't get the file" from
    "got the file, nothing readable in it".
    """
    logger.info(f"[attachments] PDF detected -- channel: {attachment.channel}, filename: {attachment.filename}")
    try:
        pdf_bytes = await attachment.fetch()
    except Exception as e:
        logger.error(f"[attachments] Failed to fetch PDF bytes: {e}", exc_info=True)
        return None
    return await extract_pdf_text(pdf_bytes)


# ---------------------------------------------------------------------------
# Shared failure copy (delivery stays per-channel; success is silent on
# GroupMe -- see groupme.py's process_pdf -- since the client already shows
# the file card natively)
# ---------------------------------------------------------------------------

def format_pdf_failure_mailto(circle: dict) -> str:
    """
    Builds the mailto: fallback text pointing at this circle's inbound
    email address, using the same "Name" <address> convention and
    display-name-not-raw-address rule as
    admin/takefive-ensemble-admin.html's openCircleDetail().
    """
    from urllib.parse import quote

    from take_five.integrations.sendgrid_email import circle_inbound_address

    display_name = circle.get("inbound_email_display_name") or circle.get("name") or "this circle"
    address = circle.get("inbound_email") or circle_inbound_address(circle["id"])
    mailbox = f'"{display_name}" <{address}>'
    mailto = f"mailto:{quote(mailbox)}"
    return f"Didn't see an attachment come through. Mind emailing it instead? {mailto}"
