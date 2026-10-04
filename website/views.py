# website/views.py
from flask import Blueprint, render_template
from .models import NewsletterPost

views = Blueprint("views", __name__)


@views.route("/")
@views.route("/Base")
def home():
    return render_template("base.html")


@views.route("/about")
def about():
    return render_template("about.html")


@views.route("/aircraft")
def aircraft():
    return render_template("aircraft.html")


@views.route("/aircraft/seabee")
def seabee():
    return render_template("seabee.html")


@views.route("/contact")
@views.route("/Contact")
def contact():
    return render_template("contact.html")


@views.route("/work-at-rsg")
def work_at_rsg():
    return render_template("work-at-rsg.html")


@views.route("/newsletter")
def newsletter():
    posts = NewsletterPost.query.order_by(NewsletterPost.date_posted.desc()).all()
    return render_template("newsletter.html", posts=posts)


@views.route("/newsletter/<int:post_id>")
def newsletter_detail(post_id):
    post = NewsletterPost.query.get_or_404(post_id)
    return render_template("newsletter_detail.html", post=post)


@views.route("/terms")
def terms():
    return render_template("terms.html")


@views.route("/privacy")
def privacy():
    return render_template("privacy.html")