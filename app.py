from flask import Flask, request
from flask_mail import Mail, Message
from psycopg import connect, OperationalError
from datetime import date, datetime, time
from dotenv import load_dotenv
from threading import Thread
import os, csv, io, logging, requests

# ==============================
# ENV + LOGGING
# ==============================
load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ==============================
# APP SETUP
# ==============================
app = Flask(__name__)

# ==============================
# MAIL SETUP
# ==============================
app.config.update(
    MAIL_SERVER=os.getenv("MAIL_SERVER"),
    MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
    MAIL_USE_TLS=True,
    MAIL_USE_SSL=False,
    MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
    MAIL_DEFAULT_SENDER=("MySqft", os.getenv("MAIL_DEFAULT_SENDER")),
)
mail = Mail(app)

# ==============================
# DATABASE
# ==============================
def get_db_conn():
    try:
        return connect(
            os.getenv("SUPABASE_DATABASE_URL"),
            sslmode="require",
            connect_timeout=5
        )
    except OperationalError as e:
        logger.error("DB connection failed: %s", e)
        return None

# ==============================
# HELPERS
# ==============================
def days_left(expiry):
    return (expiry - date.today()).days


def build_email_content(dleft):
    text = "Attached is today's lead report."
    html = "<p>Attached is today's lead report.</p>"

    if 0 <= dleft < 3:
        text += (
            f"\n\nIMPORTANT:\n"
            f"Your plan expires in {dleft} day(s).\n"
            f"Please renew to avoid service interruption."
        )

        html += f"""
        <hr>
        <p style="color:#b91c1c;">
            <strong>⚠️ IMPORTANT</strong><br>
            Your plan expires in <strong>{dleft} day(s)</strong>.<br>
            Please renew to avoid service interruption.
        </p>
        """

    return text, html


def _send_email_async(app, to, csv_content, expiry):
    with app.app_context():
        dleft = days_left(expiry)
        text_body, html_body = build_email_content(dleft)

        msg = Message(
            subject="Daily Lead Report",
            recipients=[to],
            body=text_body,
            html=html_body
        )

        msg.attach(
            filename="leads.csv",
            content_type="text/csv",
            data=csv_content
        )

        mail.send(msg)
        logger.debug("Email sent to %s", to)


def send_email(to, csv_content, expiry):
    try:
        Thread(
            target=_send_email_async,
            args=(app, to, csv_content, expiry),
            daemon=True
        ).start()
    except Exception as e:
        logger.error("Email async dispatch failed: %s", e)


def notify_discord(discord_webhook, dleft):
    if not discord_webhook:
        return

    message = (
        f"⚠️ Your subscription will expire in {dleft} day(s).\n"
        "Please renew to avoid service interruption."
    )

    try:
        requests.post(
            discord_webhook,
            json={"content": message},
            timeout=5
        )
        logger.debug("Discord notification sent")
    except Exception as e:
        logger.error("Discord notify failed: %s", e)

# ==============================
# DAILY REPORT
# ==============================
def run_report():
    today = date.today()
    today_midnight = datetime.combine(today, time.min)  # Today 00:00:00 UTC
    logger.info("Starting daily report for %s", today)

    conn = get_db_conn()
    if not conn:
        return

    try:
        cur = conn.cursor()

        # Advisory lock (prevent double run)
        cur.execute("SELECT pg_try_advisory_lock(987654321)")
        if not cur.fetchone()[0]:
            logger.warning("Report already running")
            return

        # Fetch active companies
        cur.execute("""
            SELECT id, email, plan, plan_expiry, discord_webhook
            FROM companies
            WHERE is_active = true
              AND plan_expiry >= %s
        """, (today,))
        companies = cur.fetchall()

        for cid, email, plan, expiry, discord_webhook in companies:
            try:
                dleft = days_left(expiry)
                logger.debug(
                    "Processing company | id=%s | plan=%s | expiry=%s | email=%s",
                    cid, plan, expiry, bool(email)
                )

                # Discord warning
                if dleft < 3 and plan in ("discord", "all"):
                    notify_discord(discord_webhook, dleft)

                # Fetch all leads before today
                cur.execute("""
                    SELECT lead_data
                    FROM company_leads
                    WHERE company_id = %s
                      AND created_at < %s
                    ORDER BY created_at
                """, (cid, today_midnight))
                rows = cur.fetchall()
                logger.debug("Company %s has %d leads", cid, len(rows))

                if not rows:
                    continue

                # Build CSV in memory (without created_at)
                headers = sorted({k for d, in rows for k in d})
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(headers)

                for d, in rows:
                    writer.writerow([d.get(h, "") for h in headers])

                csv_content = buf.getvalue()

                # Send email if plan allows
                if plan in ("email", "all") and email:
                    send_email(email, csv_content, expiry)

                # Delete processed leads before today
                cur.execute("""
                    DELETE FROM company_leads
                    WHERE company_id = %s
                      AND created_at < %s
                """, (cid, today_midnight))

                # Update company lead counters
                cur.execute("""
                    UPDATE companies
                    SET total_leads = total_leads + daily_leads,
                        daily_leads = 0
                    WHERE id = %s
                """, (cid,))

                conn.commit()
                logger.info("Processed company %s successfully", cid)

            except Exception:
                conn.rollback()
                logger.exception("Failed processing company %s", cid)

    except Exception:
        logger.exception("Daily report failed")
    finally:
        conn.close()
        logger.debug("Database connection closed")

    logger.info("Daily report completed")

# ==============================
# REPORT ROUTE
# ==============================
@app.route("/report")
def report():
    key = (request.headers.get("X-REPORT-KEY") or "").strip()
    expected = (os.getenv("REPORT_KEY") or "").strip()

    if key != expected:
        return {"error": "unauthorized"}, 403

    run_report()
    return {"status": "ok"}

# ==============================
# MAIN
# ==============================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
