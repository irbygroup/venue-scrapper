from datetime import datetime, timezone

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Cc, HtmlContent

from app.config import get_config


def send_email(subject: str, html_body: str, cc: bool = False) -> dict:
    """Send an email via SendGrid using config table values."""
    api_key = get_config("sendgrid_api_key")
    if not api_key:
        return {"error": "sendgrid_api_key not configured"}

    from_email = Email(get_config("email_from", "it@irbygroup.com"),
                       get_config("email_from_name", "Venue Scrapper"))
    to_email = To(get_config("email_to", "jared@irbygroup.com"))
    message = Mail(from_email=from_email, to_emails=to_email,
                   subject=subject, html_content=HtmlContent(html_body))

    if cc:
        cc_email = get_config("email_cc")
        if cc_email:
            message.add_cc(Cc(cc_email))

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        return {"status_code": response.status_code, "success": response.status_code in (200, 201, 202)}
    except Exception as e:
        return {"error": str(e)}


def notify_error(subject: str, detail: str):
    """Fire-and-forget error notification email."""
    html = f"""
    <h2 style="color:#c0392b;">⚠️ Venue Scrapper Error</h2>
    <p><strong>Time:</strong> {datetime.now(timezone.utc).isoformat()}</p>
    <p><strong>Error:</strong></p>
    <pre style="background:#f8f8f8;padding:12px;border-radius:4px;">{detail}</pre>
    """
    send_email(f"[Venue Scrapper] {subject}", html, cc=True)
