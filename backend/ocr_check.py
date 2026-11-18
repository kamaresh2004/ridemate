import re, datetime as dt
from difflib import SequenceMatcher
import cv2

DL_RE   = re.compile(r"\b([A-Z]{2}[ -]?\d{2}[ -]?(?:19|20)\d{2}[ -]?\d{7})\b")
DATE_RE = re.compile(r"\b(\d{2})[-/.](\d{2})[-/.](\d{4})\b")
EXPIRY_HINT = re.compile(r"valid|till|expiry|validity|\bNT\b|\bTR\b", re.I)

def load_gray(path):
    img = cv2.imread(path)
    if img is None:
        raise ValueError("unreadable image")
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

def blur_score(gray):
    return cv2.Laplacian(gray, cv2.CV_64F).var()   # low = blurry

def preprocess(gray):
    h, w = gray.shape
    if w < 1500:
        gray = cv2.resize(gray, None, fx=1500 / w, fy=1500 / w,
                          interpolation=cv2.INTER_CUBIC)
    gray = cv2.fastNlMeansDenoising(gray, None, 15)
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 31, 15)

def ocr_text(gray):
    import pytesseract

    return pytesseract.image_to_string(
        preprocess(gray), config="--oem 3 --psm 6", timeout=3)

def extract_licence_no(text):
    m = DL_RE.search(text.upper())
    return re.sub(r"[ -]", "", m.group(1)) if m else None

def extract_expiry(text):
    candidates = []
    for line in text.splitlines():
        if EXPIRY_HINT.search(line):
            for d, mth, y in DATE_RE.findall(line):
                try:
                    candidates.append(dt.date(int(y), int(mth), int(d)))
                except ValueError:
                    pass
    return max(candidates) if candidates else None   # latest "valid till" date

def name_score(profile_name, text):
    norm = lambda s: re.sub(r"[^a-z ]", "", s.lower()).split()
    want, words = norm(profile_name), norm(text)
    if not want or not words:
        return 0.0
    hits = [max(SequenceMatcher(None, w, t).ratio() for t in words) for w in want]
    return sum(hits) / len(hits)

def check_licence(path, profile_name, profile_licence_no=None):
    gray = load_gray(path)
    reasons = []
    if blur_score(gray) < 100:
        reasons.append("image_blurry")
    text = ocr_text(gray)
    lic, exp = extract_licence_no(text), extract_expiry(text)
    nscore = name_score(profile_name, text)

    if lic is None: reasons.append("licence_no_not_found")
    elif profile_licence_no and lic != re.sub(r"[ -]", "", profile_licence_no).upper():
        reasons.append("licence_no_mismatch")
    if exp is None: reasons.append("expiry_not_found")
    elif exp < dt.date.today(): reasons.append("expired")
    if nscore < 0.8: reasons.append("name_mismatch")

    return {"licence_no": lic, "expiry": exp.isoformat() if exp else None,
            "name_score": round(nscore, 2), "reasons": reasons,
            "result": "checks_passed" if not reasons else "needs_review"}