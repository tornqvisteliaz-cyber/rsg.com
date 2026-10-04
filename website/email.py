import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY")


def _base_html(title, body_content):
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style="margin:0; padding:0; background:#f4f6f9; font-family: Arial, Helvetica, sans-serif;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9; padding: 40px 16px;">
            <tr>
                <td align="center">
                    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px; background:#ffffff; border-radius:12px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.08);">
                        <tr>
                            <td style="background:#111827; padding:28px 32px; text-align:center;">
                                <h1 style="margin:0; color:#ffffff; font-size:22px; letter-spacing:1px;">RSG Software</h1>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding:36px 32px;">
                                <h2 style="margin:0 0 16px 0; color:#111827; font-size:24px;">{title}</h2>
                                {body_content}
                            </td>
                        </tr>
                        <tr>
                            <td style="background:#f9fafb; padding:20px 32px; text-align:center; color:#6b7280; font-size:13px;">
                                © 2026 RSG Software · High-fidelity flight simulation
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """


def send_welcome_email(to_email, name):
    if not resend.api_key:
        print("Email error: RESEND_API_KEY is missing")
        return False

    body = f"""
        <p style="margin:0 0 16px 0; color:#374151; font-size:16px; line-height:1.6;">
            Hi <strong>{name}</strong>,
        </p>
        <p style="margin:0 0 16px 0; color:#374151; font-size:16px; line-height:1.6;">
            Welcome to <strong>RSG Software</strong>! Your account has been created successfully.
        </p>
        <p style="margin:0 0 24px 0; color:#374151; font-size:16px; line-height:1.6;">
            You can now log in, manage your account and follow our latest development updates.
        </p>
        <p style="margin:0; color:#6b7280; font-size:14px; line-height:1.6;">
            Best regards,<br>
            The RSG Software Team
        </p>
    """

    try:
        result = resend.Emails.send({
            "from": "RSG Software <onboarding@resend.dev>",
            "to": [to_email],
            "subject": "Welcome to RSG Software",
            "html": _base_html("Welcome aboard!", body),
        })
        print("Welcome email sent:", result)
        return True
    except Exception as e:
        print("Email error:", repr(e))
        return False


def send_email_verification(to_email, name, verify_url):
    if not resend.api_key:
        print("Email error: RESEND_API_KEY is missing")
        return False

    body = f"""
        <p style="margin:0 0 16px 0; color:#374151; font-size:16px; line-height:1.6;">
            Hi <strong>{name}</strong>,
        </p>
        <p style="margin:0 0 16px 0; color:#374151; font-size:16px; line-height:1.6;">
            You requested to change your email address. Please confirm the new address by clicking the button below.
        </p>
        <p style="margin:0 0 28px 0; text-align:center;">
            <a href="{verify_url}"
               style="display:inline-block; background:#111827; color:#ffffff; text-decoration:none;
                      padding:14px 28px; border-radius:8px; font-size:16px; font-weight:bold;">
                Verify email address
            </a>
        </p>
        <p style="margin:0 0 8px 0; color:#6b7280; font-size:13px; line-height:1.6;">
            If you did not request this change, you can ignore this email.
        </p>
        <p style="margin:0; color:#9ca3af; font-size:12px; line-height:1.5; word-break:break-all;">
            Or open this link: {verify_url}
        </p>
    """

    try:
        result = resend.Emails.send({
            "from": "RSG Software <onboarding@resend.dev>",
            "to": [to_email],
            "subject": "Verify your new email – RSG Software",
            "html": _base_html("Verify your email", body),
        })
        print("Verification email sent:", result)
        return True
    except Exception as e:
        print("Email error:", repr(e))
        return False


def send_delete_account_email(to_email, name, delete_url):
    if not resend.api_key:
        print("Email error: RESEND_API_KEY is missing")
        return False

    body = f"""
        <p style="margin:0 0 16px 0; color:#374151; font-size:16px; line-height:1.6;">
            Hi <strong>{name}</strong>,
        </p>
        <p style="margin:0 0 16px 0; color:#374151; font-size:16px; line-height:1.6;">
            We received a request to <strong>permanently delete</strong> your RSG Software account.
        </p>
        <p style="margin:0 0 16px 0; color:#374151; font-size:16px; line-height:1.6;">
            This action cannot be undone. All your account data will be removed.
        </p>
        <p style="margin:0 0 28px 0; text-align:center;">
            <a href="{delete_url}"
               style="display:inline-block; background:#dc2626; color:#ffffff; text-decoration:none;
                      padding:14px 28px; border-radius:8px; font-size:16px; font-weight:bold;">
                Confirm account deletion
            </a>
        </p>
        <p style="margin:0 0 8px 0; color:#6b7280; font-size:13px; line-height:1.6;">
            If you did not request this, you can safely ignore this email. Your account will stay active.
        </p>
        <p style="margin:0; color:#9ca3af; font-size:12px; line-height:1.5; word-break:break-all;">
            Or open this link: {delete_url}
        </p>
    """

    try:
        result = resend.Emails.send({
            "from": "RSG Software <onboarding@resend.dev>",
            "to": [to_email],
            "subject": "Confirm account deletion – RSG Software",
            "html": _base_html("Delete your account?", body),
        })
        print("Delete account email sent:", result)
        return True
    except Exception as e:
        print("Email error:", repr(e))
        return False