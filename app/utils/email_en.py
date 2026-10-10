"""Corpo delle email di notifica, in inglese.

La versione italiana resta in notification_email.generate_email_html.
Qui si entra solo quando chi riceve ha scelto l'inglese.
"""
from typing import Dict, Optional

from app.utils.orari import ORE_RILASCIO_SENZA_RECENSIONE
from app.utils.notification_email import (
    _branded_email,
    _details_box,
    _fill,
    nota_compenso,
)


def email_html_en(template_name: str, data: Dict[str, str]) -> Optional[str]:
    GREEN, GREEN_D = "#43a047", "#2e7d32"
    AMBER, AMBER_D = "#f39c12", "#e67e22"
    RED, RED_D = "#e74c3c", "#c0392b"
    GOLD, GOLD_D = "#f5a623", "#e8900c"
    ore = ORE_RILASCIO_SENZA_RECENSIONE

    def row(label, value):
        return f'<p style="margin:6px 0;color:#374151;"><strong>{label}</strong> {value}</p>'

    def mail(emoji, title, accent, dark, body, button, url):
        return _fill(
            _branded_email(emoji, title, accent, dark, body, button, url, lingua="en"),
            data,
        )

    if template_name == "booking_confirmed.html":
        body = (
            "<p>Hello <strong>{consultant_name}</strong>,</p>"
            "<p>You have a new booking from <strong>{client_name}</strong>.</p>"
            + _details_box(
                row("Date:", "{date}") + row("Time:", "{time}") + row("Length:", "{duration} minutes"),
                GREEN,
            )
            + "<p>Get ready in advance and be on time, so your client has a good session.</p>"
            + nota_compenso("en")
        )
        return mail("📅", "New booking", GREEN, GREEN_D, body, "View booking", "{action_url}")

    if template_name == "reminder_1h.html":
        body = (
            "<p>Hello <strong>{user_name}</strong>,</p>"
            "<p>Your consultation with <strong>{other_user_name}</strong> starts in <strong>1 hour</strong>.</p>"
            + _details_box(
                row("Date:", "{date}") + row("Start time:", "{time}") + row("Length:", "{duration} minutes"),
                AMBER,
            )
            + "<p><strong>Tip:</strong> test your audio and video before it starts.</p>"
        )
        return mail("⏰", "Consultation reminder", AMBER, AMBER_D, body, "Go to the booking", "{action_url}")

    if template_name == "reminder_10min.html":
        body = (
            "<p>Hello <strong>{user_name}</strong>,</p>"
            "<p style='font-size:18px;font-weight:bold;color:#c0392b;'>Your consultation starts in 10 MINUTES.</p>"
            + _details_box(
                row("With:", "{other_user_name}") + row("Time:", "{time}") + row("Length:", "{duration} minutes"),
                RED,
            )
            + "<p>Check audio and video. Find a quiet place. Keep any useful notes nearby.</p>"
        )
        return mail("🚀", "Consultation starting", RED, RED_D, body, "Join the room", "{action_url}")

    if template_name == "community_contact.html":
        body = (
            "<p>Hello <strong>{author_name}</strong>,</p>"
            "<p>Someone is interested in your question and wants to contact you.</p>"
            + _details_box(
                row("From:", "{contact_name}") + row("Your question:", "{question_title}") + row("When:", "{contact_date}"),
                GREEN,
            )
            + "<p>You will see their messages in the Ispiramy chat. A quick reply makes a consultation more likely.</p>"
        )
        return mail("💬", "New message from the community", GREEN, GREEN_D, body, "Open chat", "{action_url}")

    if template_name == "booking_refused.html":
        body = (
            "<p>Hello <strong>{client_name}</strong>,</p>"
            "<p><strong>{consultant_name}</strong> declined the consultation you booked.</p>"
            + _details_box(
                row("Date:", "{date}") + row("Time:", "{time}") + row("Consultant:", "{consultant_name}"),
                RED,
            )
            + "{reason_section}"
            + '<div style="background:#e8f5e9;border-left:4px solid #43a047;border-radius:6px;padding:14px 18px;margin:20px 0;">'
              "<p style='margin:0;'><strong>Payment:</strong> {refund_note}</p></div>"
            + "<p>You can look for another consultant in the catalogue.</p>"
        )
        return mail("⚠️", "Consultation declined", RED, RED_D, body, "Find another consultant", "{action_url}")

    if template_name == "booking_confirmed_client.html":
        body = (
            "<p>Hello <strong>{client_name}</strong>,</p>"
            "<p>Your consultation with <strong>{consultant_name}</strong> is confirmed and paid.</p>"
            + _details_box(
                row("Date:", "{date}") + row("Time:", "{time}") + row("Length:", "{duration} minutes"),
                GREEN,
            )
            + "<p>You will get a reminder before it starts. At the scheduled time, join the call from your profile: "
              "the button appears 10 minutes before.</p>"
        )
        return mail("✅", "Consultation confirmed", GREEN, GREEN_D, body, "Go to your consultations", "{action_url}")

    if template_name == "booking_cancelled.html":
        body = (
            "<p>Hello <strong>{user_name}</strong>,</p>"
            "<p><strong>{other_name}</strong> cancelled the consultation you had scheduled.</p>"
            + _details_box(row("Date:", "{date}") + row("Time:", "{time}"), RED)
            + "{reason_section}"
            + '<div style="background:#e8f5e9;border-left:4px solid #43a047;border-radius:6px;padding:14px 18px;margin:20px 0;">'
              "<p style='margin:0;'><strong>Payment:</strong> {refund_note}</p></div>"
        )
        return mail("❌", "Consultation cancelled", RED, RED_D, body, "Go to your profile", "{action_url}")

    if template_name == "dispute_opened.html":
        body = (
            "<p>Hello <strong>{consultant_name}</strong>,</p>"
            "<p><strong>{client_name}</strong> opened a dispute about the consultation you had.</p>"
            + _details_box(row("Date:", "{date}") + row("Time:", "{time}"), AMBER)
            + "{reason_section}"
            + '<div style="background:#fff8e1;border-left:4px solid #f39c12;border-radius:6px;padding:14px 18px;margin:20px 0;">'
              "<p style='margin:0;'>This payout stays on hold until the dispute is closed. "
              "With no open dispute, it is sent when the client leaves a review, or "
              f"{ore} hours after the consultation ends if they never review. "
              "We will contact you if we need more detail. If the consultation was recorded, "
              "the recording is used to check what happened.</p></div>"
        )
        return mail("⚠️", "Dispute opened", AMBER, AMBER_D, body, "Go to your profile", "{action_url}")

    if template_name == "dispute_decided.html":
        body = (
            "<p>Hello <strong>{user_name}</strong>,</p>"
            "<p>We closed the dispute about the consultation on <strong>{date}</strong>"
            " at <strong>{time}</strong>.</p>"
            + '<div style="background:#f1f8f0;border-left:4px solid #43a047;border-radius:6px;'
              'padding:14px 18px;margin:20px 0;">'
            "<p style='margin:0;'>{outcome_en}</p></div>"
        )
        return mail("⚖️", "Decision on the dispute", GREEN, GREEN_D, body, "Go to your profile", "{action_url}")

    if template_name == "booking_request.html":
        body = (
            "<p>Hello <strong>{consultant_name}</strong>,</p>"
            "<p><strong>{client_name}</strong> asked you for a consultation. You chose to confirm bookings yourself: "
            "the consultation happens only if you accept.</p>"
            + _details_box(
                row("Date:", "{date}") + row("Time:", "{time}") + row("Length:", "{duration} minutes")
                + row("Topic:", "{topic}"),
                AMBER,
            )
            + '<div style="background:#fff8e1;border-left:4px solid #f39c12;border-radius:6px;padding:14px 18px;margin:20px 0;">'
              "<p style='margin:0;'><strong>Reply by {deadline}.</strong> The client's payment is only authorized "
              "and is captured when you accept. If you decline or do not reply in time, the hold is cancelled.</p></div>"
        )
        return mail("📩", "New consultation request", AMBER, AMBER_D, body, "Accept or decline", "{action_url}")

    if template_name == "booking_accepted.html":
        body = (
            "<p>Hello <strong>{client_name}</strong>,</p>"
            "<p><strong>{consultant_name}</strong> accepted your request: the consultation is confirmed.</p>"
            + _details_box(
                row("Date:", "{date}") + row("Time:", "{time}") + row("Length:", "{duration} minutes"),
                GREEN,
            )
            + "<p>The amount that was on hold has now been charged. You will get a reminder before it starts: "
              "join the call from your profile at the scheduled time.</p>"
        )
        return mail("✅", "Consultation confirmed", GREEN, GREEN_D, body, "Go to your consultations", "{action_url}")

    if template_name == "booking_request_expired.html":
        body = (
            "<p>Hello <strong>{client_name}</strong>,</p>"
            "<p><strong>{consultant_name}</strong> did not reply in time to your consultation request, "
            "so it was cancelled.</p>"
            + _details_box(row("Date:", "{date}") + row("Time:", "{time}"), AMBER)
            + '<div style="background:#e8f5e9;border-left:4px solid #43a047;border-radius:6px;padding:14px 18px;margin:20px 0;">'
              "<p style='margin:0;'><strong>No charge:</strong> the amount was only authorized and the hold was cancelled. "
              "Depending on your bank it may stay visible on your statement for a few days.</p></div>"
            + "<p>You can book with another available consultant.</p>"
        )
        return mail("⌛", "Request not confirmed", AMBER, AMBER_D, body, "Find a consultant", "{action_url}")

    if template_name == "review_request.html":
        body = (
            "<p>Hello <strong>{user_name}</strong>,</p>"
            "<p>How did your consultation with <strong>{consultant_name}</strong> go?</p>"
            + _details_box(row("Date:", "{date}") + row("Time:", "{time}"), GOLD)
            + "<p>Your opinion helps other people choose the right consultant. It takes about 30 seconds.</p>"
            + nota_compenso("en")
        )
        return mail("⭐", "Leave a review", GOLD, GOLD_D, body, "Leave your review", "{review_url}")

    if template_name == "review_reminder.html":
        body = (
            "<p>Hello <strong>{user_name}</strong>,</p>"
            "<p>You have not left a review yet for your consultation with <strong>{consultant_name}</strong>.</p>"
            + _details_box(row("Date:", "{date}") + row("Time:", "{time}"), AMBER)
            + "<p>It takes about 30 seconds and helps other people find the right consultant. If you already did, ignore this message.</p>"
            + nota_compenso("en")
        )
        return mail("🔔", "Don't forget your review", AMBER, AMBER_D, body, "Leave your review", "{review_url}")

    if template_name == "review_received.html":
        body = (
            "<p>Hello <strong>{consultant_name}</strong>,</p>"
            "<p><strong>{reviewer_name}</strong> left you a review for your consultation.</p>"
            + _details_box(row("Rating:", "{rating}/5") + row("Consultation on:", "{date}"), GOLD)
            + '<div style="background:#fffbeb;border-left:4px solid #f5a623;border-radius:6px;padding:14px 18px;margin:20px 0;font-style:italic;color:#6b7280;">“{comment}”</div>'
            + "<p>Reviews make you easier to find and build trust with future clients.</p>"
        )
        return mail("⭐", "You received a review", GOLD, GOLD_D, body, "See your profile", "{action_url}")

    return None
