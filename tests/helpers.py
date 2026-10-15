"""Small real-PDF and HTTP helpers shared by focused integration tests."""
from io import BytesIO


def make_pdf(text="SourceDesk stores documents with accurate page citations.", *, pages=None):
    from reportlab.pdfgen.canvas import Canvas
    output = BytesIO()
    canvas = Canvas(output)
    for page_text in pages if pages is not None else [text]:
        canvas.drawString(72, 720, page_text)
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def register_and_login(client, email="owner@example.com", password="correct-horse-password-123"):
    response = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    token = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert token.status_code == 200, token.text
    return {"Authorization": "Bearer " + token.json()["access_token"]}


def create_collection(client, headers, name="Research"):
    response = client.post("/api/v1/collections", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def upload_document(client, headers, collection_id, data, filename="research.pdf"):
    return client.post(f"/api/v1/collections/{collection_id}/documents", headers=headers,
                       files={"file": (filename, data, "application/pdf")})
