import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def main():
    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("BRIEFING_TO")

    if not gmail_address:
        raise RuntimeError("GMAIL_ADDRESS 환경변수가 없습니다.")

    if not gmail_app_password:
        raise RuntimeError("GMAIL_APP_PASSWORD 환경변수가 없습니다.")

    if not recipient:
        raise RuntimeError("BRIEFING_TO 환경변수가 없습니다.")

    briefing_path = Path("briefing.md")

    if not briefing_path.exists():
        raise RuntimeError("briefing.md 파일이 없습니다.")

    briefing = briefing_path.read_text(
        encoding="utf-8"
    )

    msg = EmailMessage()

    msg["Subject"] = "미국 주식 포트폴리오 브리핑"
    msg["From"] = gmail_address
    msg["To"] = recipient

    msg.set_content(briefing)

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
    ) as smtp:

        smtp.login(
            gmail_address,
            gmail_app_password,
        )

        smtp.send_message(msg)

    print(
        f"Briefing email sent to {recipient}"
    )


if __name__ == "__main__":
    main()