from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.storage.blob import BlobServiceClient
from azure.core.credentials import AzureKeyCredential
import os
import json
import uuid

DOC_ENDPOINT = os.environ["DOC_ENDPOINT"]
DOC_KEY = os.environ["DOC_KEY"]
STORAGE_CONN = os.environ["STORAGE_CONN"]

doc_client = DocumentIntelligenceClient(
    DOC_ENDPOINT, AzureKeyCredential(DOC_KEY)
)
blob_service = BlobServiceClient.from_connection_string(STORAGE_CONN)

def ocr_and_chunk(blob_name: str):
    source = blob_service.get_blob_client("textbooks-raw", blob_name)
    pdf_bytes = source.download_blob().readall()

    poller = doc_client.begin_analyze_document(
        "prebuilt-layout",
        AnalyzeDocumentRequest(bytes_source=pdf_bytes)
    )
    result = poller.result()

    chunks = []
    for i, para in enumerate(result.paragraphs or []):
        if len(para.content.strip()) < 30:
            continue
        chunk = {
            "chunk_id": f"{blob_name}-{i:04d}",
            "source_file": blob_name,
            "text_content": para.content.strip(),
            "page": para.bounding_regions[0].page_number if para.bounding_regions else 0
        }
        chunks.append(chunk)

    output_name = blob_name.replace(".pdf", "_chunks.json")
    dest = blob_service.get_blob_client("chunks-pending", output_name)
    dest.upload_blob(json.dumps(chunks, ensure_ascii=False, indent=2), overwrite=True)
    print(f"Записани {len(chunks)} chunk-а в chunks-pending/{output_name}")
    return len(chunks)
