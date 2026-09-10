import os
import base64
import json
import time
from pathlib import Path
import openai
import fitz  # PyMuPDF for PDF processing

# Configuration
DATA_DIR = "data"
OUTPUT_DIR = "output"
API_BASE_URL = "http://localhost:1234/v1"
MODEL_NAME = "google/gemma-3-4b"
API_KEY = "not-needed"  # LM Studio ignores this

# Create output directory if it doesn't exist
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Initialize OpenAI client pointing to LM Studio
client = openai.OpenAI(
    base_url=API_BASE_URL,
    api_key=API_KEY,
)

# Supported image extensions
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".pdf"}

def encode_image_to_base64(image_bytes: bytes) -> str:
    """Return base64-encoded string from image bytes."""
    return base64.b64encode(image_bytes).decode("utf-8")

def get_mime_type(file_path: str) -> str:
    """Return MIME type based on file extension."""
    ext = Path(file_path).suffix.lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".pdf": "application/pdf",
    }
    return mapping.get(ext, "application/octet-stream")

def clean_markdown_response(text: str) -> str:
    """
    Remove common introductory phrases from LLM output.
    Looks for the first line that starts with a Markdown heading (#),
    or with bold text (e.g., **Description**), and returns from there.
    Also strips trailing code fences if present.
    """
    if not text:
        return ""
    lines = text.splitlines()
    start_idx = 0
    # Try to find the first line that looks like actual content
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("**"):
            start_idx = i
            break
        if i > 5:
            start_idx = i
            break
    content = "\n".join(lines[start_idx:]).strip()
    if content.endswith("```"):
        content = content[:-3].rstrip()
    return content

def send_to_model(image_bytes: bytes, mime_type: str) -> str | None:
    """Send image (bytes) to the vision model and return cleaned Markdown."""
    try:
        base64_image = encode_image_to_base64(image_bytes)
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Analyze this document image. Provide a Markdown output with the following sections:\n"
                            "1. **Description**: A brief description of the document type and its content.\n"
                            "2. **Extracted Text**: All text visible in the image, transcribed exactly.\n"
                            "3. **Tables** (if any): Any tabular data formatted as Markdown tables.\n"
                            "Output only the Markdown content. Do not include any introductory or concluding sentences."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{base64_image}"
                        },
                    },
                ],
            }
        ]

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.2,
            max_tokens=4000,
        )

        if response.choices and response.choices[0].message.content:
            raw = response.choices[0].message.content.strip()
            return clean_markdown_response(raw)
        else:
            print("  No content returned.")
            return None

    except Exception as e:
        print(f"  Error processing image: {e}")
        return None

def process_image_file(image_path: str) -> str | None:
    """Process a single image file and return Markdown."""
    print(f"Processing image: {image_path}")
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    mime_type = get_mime_type(image_path)
    return send_to_model(image_bytes, mime_type)

def process_pdf_file(pdf_path: str) -> str | None:
    """Process a PDF by rendering each page as an image and extracting info."""
    print(f"Processing PDF: {pdf_path}")
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"  Failed to open PDF: {e}")
        return None

    all_markdown_parts = []
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        mime_type = "image/png"
        print(f"  Processing page {page_num + 1}/{len(doc)}")
        page_markdown = send_to_model(img_bytes, mime_type)
        if page_markdown:
            all_markdown_parts.append(f"## Page {page_num + 1}\n\n{page_markdown}")
        else:
            all_markdown_parts.append(f"## Page {page_num + 1}\n\n*Failed to extract content.*")
    doc.close()

    if all_markdown_parts:
        return "\n\n".join(all_markdown_parts)
    else:
        return None

def main():
    data_path = Path(DATA_DIR)
    if not data_path.exists():
        print(f"Data directory '{DATA_DIR}' not found.")
        return

    for file_path in data_path.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in IMAGE_EXTENSIONS:
            start_time = time.time()
            
            if file_path.suffix.lower() == ".pdf":
                markdown_content = process_pdf_file(str(file_path))
            else:
                markdown_content = process_image_file(str(file_path))
            
            elapsed = time.time() - start_time

            if markdown_content:
                out_file = Path(OUTPUT_DIR) / f"{file_path.stem}.md"
                with open(out_file, "w", encoding="utf-8") as f:
                    f.write(markdown_content)
                print(f"  Saved Markdown to {out_file}")
            else:
                print(f"  No content generated for {file_path.name}")

            print(f"  Time taken: {elapsed:.2f} seconds\n")

if __name__ == "__main__":
    main()