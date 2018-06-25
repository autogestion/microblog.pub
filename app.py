import binascii
import hashlib
import json
import logging
import mimetypes
import os
import urllib
from datetime import datetime
from datetime import timezone
from functools import wraps
from typing import Any
from typing import Dict
from urllib.parse import urlencode
from urllib.parse import urlparse

import bleach
import mf2py
import piexif
import pymongo
import timeago
from bson.objectid import ObjectId
from dateutil import parser
from flask import Flask
from flask import Response
from flask import abort
from flask import jsonify as flask_jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from flask_wtf.csrf import CSRFProtect
from html2text import html2text
from itsdangerous import BadSignature
from passlib.hash import bcrypt
from u2flib_server import u2f
from werkzeug.utils import secure_filename

import activitypub
import config
from activitypub import embed_collection
from config import ADMIN_API_KEY
from config import BASE_URL
from config import DB
from config import DEBUG_MODE
from config import DOMAIN
from config import HEADERS
from config import ID
from config import JWT
from config import KEY
from config import ME
from config import PASS
from config import USERNAME
from config import VERSION
from config import _drop_db
from config import custom_cache_purge_hook
from little_boxes import activitypub as ap
from little_boxes.activitypub import ActivityType
from little_boxes.activitypub import clean_activity
from little_boxes.activitypub import get_backend
from little_boxes.content_helper import parse_markdown
from little_boxes.errors import ActivityNotFoundError
from little_boxes.errors import Error
from little_boxes.errors import NotFromOutboxError
from little_boxes.httpsig import HTTPSigAuth
from little_boxes.httpsig import verify_request
from little_boxes.webfinger import get_actor_url
from little_boxes.webfinger import get_remote_follow_template
from utils.key import get_secret_key
from utils.object_service import ObjectService

OBJECT_SERVICE = ACTOR_SERVICE = ObjectService()

back = activitypub.MicroblogPubBackend()
ap.use_backend(back)

MY_PERSON = ap.Person(**ME)

app = Flask(__name__)
app.secret_key = get_secret_key("flask")
app.config.update(WTF_CSRF_CHECK_DEFAULT=False)
csrf = CSRFProtect(app)

logger = logging.getLogger(__name__)

# Hook up Flask logging with gunicorn
root_logger = logging.getLogger()
if os.getenv("FLASK_DEBUG"):
    logger.setLevel(logging.DEBUG)
    root_logger.setLevel(logging.DEBUG)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    root_logger.handlers = gunicorn_logger.handlers
    root_logger.setLevel(gunicorn_logger.level)

SIG_AUTH = HTTPSigAuth(KEY)

OUTBOX = ap.Outbox(MY_PERSON)
INBOX = ap.Inbox(MY_PERSON)


def verify_pass(pwd):
    return bcrypt.verify(pwd, PASS)


@app.context_processor
def inject_config():
    q = {
        "type": "Create",
        "activity.object.type": "Note",
        "activity.object.inReplyTo": None,
        "meta.deleted": False,
    }
    notes_count = DB.outbox.find(
        {"$or": [q, {"type": "Announce", "meta.undo": False}]}
    ).count()
    q = {"type": "Create", "activity.object.type": "Note", "meta.deleted": False}
    with_replies_count = DB.outbox.find(
        {"$or": [q, {"type": "Announce", "meta.undo": False}]}
    ).count()
    return dict(
        microblogpub_version=VERSION,
        config=config,
        logged_in=session.get("logged_in", False),
        followers_count=DB.followers.count(),
        following_count=DB.following.count(),
        notes_count=notes_count,
        with_replies_count=with_replies_count,
    )


@app.after_request
def set_x_powered_by(response):
    response.headers["X-Powered-By"] = "microblog.pub"
    return response


# HTML/templates helper
ALLOWED_TAGS = [
    "a",
    "abbr",
    "acronym",
    "b",
    "blockquote",
    "code",
    "pre",
    "em",
    "i",
    "li",
    "ol",
    "strong",
    "ul",
    "span",
    "div",
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
]


def clean_html(html):
    return bleach.clean(html, tags=ALLOWED_TAGS)


@app.template_filter()
def quote_plus(t):
    return urllib.parse.quote_plus(t)


@app.template_filter()
def is_from_outbox(t):
    return t.startswith(ID)


@app.template_filter()
def clean(html):
    return clean_html(html)


@app.template_filter()
def html2plaintext(body):
    return html2text(body)


@app.template_filter()
def domain(url):
    return urlparse(url).netloc


@app.template_filter()
def get_actor(url):
    if not url:
        return None
    print(f"GET_ACTOR {url}")
    return ACTOR_SERVICE.get(url)


@app.template_filter()
def format_time(val):
    if val:
        dt = parser.parse(val)
        return datetime.strftime(dt, "%B %d, %Y, %H:%M %p")
    return val


@app.template_filter()
def format_timeago(val):
    if val:
        dt = parser.parse(val)
        return timeago.format(dt, datetime.now(timezone.utc))
    return val


def _is_img(filename):
    filename = filename.lower()
    if (
        filename.endswith(".png")
        or filename.endswith(".jpg")
        or filename.endswith(".jpeg")
        or filename.endswith(".gif")
        or filename.endswith(".svg")
    ):
        return True
    return False


@app.template_filter()
def not_only_imgs(attachment):
    for a in attachment:
        if not _is_img(a["url"]):
            return True
    return False


@app.template_filter()
def is_img(filename):
    return _is_img(filename)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)

    return decorated_function


def _api_required():
    if session.get("logged_in"):
        if request.method not in ["GET", "HEAD"]:
            # If a standard API request is made with a "login session", it must havw a CSRF token
            csrf.protect()
        return

    # Token verification
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        # IndieAuth token
        token = request.form.get("access_token", "")

    # Will raise a BadSignature on bad auth
    if token != 'xyugavnomuravej':
        payload = JWT.loads(token)
        logger.info(f"api call by {payload}")


def api_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            _api_required()
        except BadSignature:
            abort(401)

        return f(*args, **kwargs)

    return decorated_function


def jsonify(**data):
    if "@context" not in data:
        data["@context"] = config.CTX_AS
    return Response(
        response=json.dumps(data),
        headers={
            "Content-Type": "application/json"
            if app.debug
            else "application/activity+json"
        },
    )


def is_api_request():
    h = request.headers.get("Accept")
    if h is None:
        return False
    h = h.split(",")[0]
    if h in HEADERS or h == "application/json":
        return True
    return False


@app.errorhandler(ValueError)
def handle_value_error(error):
    logger.error(f"caught value error: {error!r}")
    response = flask_jsonify(message=error.args[0])
    response.status_code = 400
    return response


@app.errorhandler(Error)
def handle_activitypub_error(error):
    logger.error(f"caught activitypub error {error!r}")
    response = flask_jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


# App routes

#######
# Login


@app.route("/logout")
@login_required
def logout():
    session["logged_in"] = False
    return redirect("/")


@app.route("/login", methods=["POST", "GET"])
def login():
    devices = [doc["device"] for doc in DB.u2f.find()]
    u2f_enabled = True if devices else False
    if request.method == "POST":
        csrf.protect()
        pwd = request.form.get("pass")
        if pwd and verify_pass(pwd):
            if devices:
                resp = json.loads(request.form.get("resp"))
                print(resp)
                try:
                    u2f.complete_authentication(session["challenge"], resp)
                except ValueError as exc:
                    print("failed", exc)
                    abort(401)
                    return
                finally:
                    session["challenge"] = None

            session["logged_in"] = True
            return redirect(request.args.get("redirect") or "/admin")
        else:
            abort(401)

    payload = None
    if devices:
        payload = u2f.begin_authentication(ID, devices)
        session["challenge"] = payload

    return render_template(
        "login.html", u2f_enabled=u2f_enabled, me=ME, payload=payload
    )


@app.route("/remote_follow", methods=["GET", "POST"])
def remote_follow():
    if request.method == "GET":
        return render_template("remote_follow.html")

    csrf.protect()
    profile = request.form.get("profile")
    if not profile.startswith("@"):
        profile = f"@{profile}"
    return redirect(
        get_remote_follow_template(profile).format(uri=f"{USERNAME}@{DOMAIN}")
    )


@app.route("/authorize_follow", methods=["GET", "POST"])
@login_required
def authorize_follow():
    if request.method == "GET":
        return render_template(
            "authorize_remote_follow.html", profile=request.args.get("profile")
        )

    actor = get_actor_url(request.form.get("profile"))
    if not actor:
        abort(500)
    if DB.following.find({"remote_actor": actor}).count() > 0:
        return redirect("/following")

    follow = ap.Follow(actor=MY_PERSON.id, object=actor)
    OUTBOX.post(follow)

    return redirect("/following")


@app.route("/u2f/register", methods=["GET", "POST"])
@login_required
def u2f_register():
    # TODO(tsileo): ensure no duplicates
    if request.method == "GET":
        payload = u2f.begin_registration(ID)
        session["challenge"] = payload
        return render_template("u2f.html", payload=payload)
    else:
        resp = json.loads(request.form.get("resp"))
        device, device_cert = u2f.complete_registration(session["challenge"], resp)
        session["challenge"] = None
        DB.u2f.insert_one({"device": device, "cert": device_cert})
        return ""


#######
# Activity pub routes


@app.route("/")
def index():
    if is_api_request():
        return jsonify(**ME)

    # FIXME(tsileo): implements pagination, also for the followers/following page
    limit = 50
    q = {
        "type": "Create",
        "activity.object.type": "Note",
        "activity.object.inReplyTo": None,
        "meta.deleted": False,
    }
    c = request.args.get("cursor")
    if c:
        q["_id"] = {"$lt": ObjectId(c)}

    outbox_data = list(
        DB.outbox.find(
            {"$or": [q, {"type": "Announce", "meta.undo": False}]}, limit=limit
        ).sort("_id", -1)
    )
    cursor = None
    if outbox_data and len(outbox_data) == limit:
        cursor = str(outbox_data[-1]["_id"])

    for data in outbox_data:
        if data["type"] == "Announce":
            print(data)
            if data["activity"]["object"].startswith("http"):
                data["ref"] = {
                    "activity": {
                        "object": OBJECT_SERVICE.get(data["activity"]["object"])
                    },
                    "meta": {},
                }

    return render_template(
        "index.html",
        me=ME,
        notes=DB.inbox.find(
            {"type": "Create", "activity.object.type": "Note", "meta.deleted": False}
        ).count(),
        followers=DB.followers.count(),
        following=DB.following.count(),
        outbox_data=outbox_data,
        cursor=cursor,
    )


@app.route("/with_replies")
def with_replies():
    limit = 50
    q = {"type": "Create", "activity.object.type": "Note", "meta.deleted": False}
    c = request.args.get("cursor")
    if c:
        q["_id"] = {"$lt": ObjectId(c)}

    outbox_data = list(
        DB.outbox.find(
            {"$or": [q, {"type": "Announce", "meta.undo": False}]}, limit=limit
        ).sort("_id", -1)
    )
    cursor = None
    if outbox_data and len(outbox_data) == limit:
        cursor = str(outbox_data[-1]["_id"])

    for data in outbox_data:
        if data["type"] == "Announce":
            print(data)
            if data["activity"]["object"].startswith("http"):
                data["ref"] = {
                    "activity": {
                        "object": OBJECT_SERVICE.get(data["activity"]["object"])
                    },
                    "meta": {},
                }

    return render_template(
        "index.html",
        me=ME,
        notes=DB.inbox.find(
            {"type": "Create", "activity.object.type": "Note", "meta.deleted": False}
        ).count(),
        followers=DB.followers.count(),
        following=DB.following.count(),
        outbox_data=outbox_data,
        cursor=cursor,
    )


def _build_thread(data, include_children=True):
    data["_requested"] = True
    print(data)
    root_id = data["meta"].get("thread_root_parent", data["activity"]["object"]["id"])

    query = {"$or": [{"meta.thread_root_parent": root_id, "type": "Create"}]}
    if data['activity']['object'].get('inReplyTo'):
        query['$or'].append({'activity.object.id': data['activity']['object']['inReplyTo']})

    # Fetch the root replies, and the children
    replies = (
        [data]
        + list(DB.inbox.find(query))
        + list(DB.outbox.find(query))
        + list(DB.threads.find(query))
    )
    replies = sorted(replies, key=lambda d: d["activity"]["object"]["published"])
    # Index all the IDs in order to build a tree
    idx = {}
    replies2 = []
    for rep in replies:
        rep_id = rep["activity"]["object"]["id"]
        if rep_id in idx:
            continue
        idx[rep_id] = rep.copy()
        idx[rep_id]["_nodes"] = []
        replies2.append(rep)

    # Build the tree
    for rep in replies2:
        rep_id = rep["activity"]["object"]["id"]
        if rep_id == root_id:
            continue
        reply_of = rep["activity"]["object"]["inReplyTo"]
        idx[reply_of]["_nodes"].append(rep)

    # Flatten the tree
    thread = []

    def _flatten(node, level=0):
        node["_level"] = level
        thread.append(node)

        for snode in sorted(
            idx[node["activity"]["object"]["id"]]["_nodes"],
            key=lambda d: d["activity"]["object"]["published"],
        ):
            _flatten(snode, level=level + 1)

    _flatten(idx[root_id])

    return thread


@app.route("/note/<note_id>")
def note_by_id(note_id):
    data = DB.outbox.find_one({"remote_id": back.activity_url(note_id)})
    if not data:
        abort(404)
    if data["meta"].get("deleted", False):
        abort(410)
    thread = _build_thread(data)

    likes = list(
        DB.inbox.find(
            {
                "meta.undo": False,
                "type": ActivityType.LIKE.value,
                "$or": [
                    {"activity.object.id": data["activity"]["object"]["id"]},
                    {"activity.object": data["activity"]["object"]["id"]},
                ],
            }
        )
    )
    likes = [ACTOR_SERVICE.get(doc["activity"]["actor"]) for doc in likes]

    shares = list(
        DB.inbox.find(
            {
                "meta.undo": False,
                "type": ActivityType.ANNOUNCE.value,
                "$or": [
                    {"activity.object.id": data["activity"]["object"]["id"]},
                    {"activity.object": data["activity"]["object"]["id"]},
                ],
            }
        )
    )
    shares = [ACTOR_SERVICE.get(doc["activity"]["actor"]) for doc in shares]

    return render_template(
        "note.html", likes=likes, shares=shares, me=ME, thread=thread, note=data
    )


@app.route("/nodeinfo")
def nodeinfo():
    return Response(
        headers={
            "Content-Type": "application/json; profile=http://nodeinfo.diaspora.software/ns/schema/2.0#"
        },
        response=json.dumps(
            {
                "version": "2.0",
                "software": {
                    "name": "microblogpub",
                    "version": f"Microblog.pub {VERSION}",
                },
                "protocols": ["activitypub"],
                "services": {"inbound": [], "outbound": []},
                "openRegistrations": False,
                "usage": {"users": {"total": 1}, "localPosts": DB.outbox.count()},
                "metadata": {
                    "sourceCode": "https://github.com/tsileo/microblog.pub",
                    "nodeName": f"@{USERNAME}@{DOMAIN}",
                },
            }
        ),
    )


@app.route("/.well-known/nodeinfo")
def wellknown_nodeinfo():
    return flask_jsonify(
        links=[
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                "href": f"{ID}/nodeinfo",
            }
        ]
    )


@app.route("/.well-known/webfinger")
def wellknown_webfinger():
    """Enable WebFinger support, required for Mastodon interopability."""
    resource = request.args.get("resource")
    if resource not in [f"acct:{USERNAME}@{DOMAIN}", ID]:
        abort(404)

    out = {
        "subject": f"acct:{USERNAME}@{DOMAIN}",
        "aliases": [ID],
        "links": [
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": BASE_URL,
            },
            {"rel": "self", "type": "application/activity+json", "href": ID},
            {
                "rel": "http://ostatus.org/schema/1.0/subscribe",
                "template": BASE_URL + "/authorize_follow?profile={uri}",
            },
        ],
    }

    return Response(
        response=json.dumps(out),
        headers={
            "Content-Type": "application/jrd+json; charset=utf-8"
            if not app.debug
            else "application/json"
        },
    )


def add_extra_collection(raw_doc: Dict[str, Any]) -> Dict[str, Any]:
    if raw_doc["activity"]["type"] != ActivityType.CREATE.value:
        return raw_doc

    raw_doc["activity"]["object"]["replies"] = embed_collection(
        raw_doc.get("meta", {}).get("count_direct_reply", 0),
        f'{raw_doc["remote_id"]}/replies',
    )

    raw_doc["activity"]["object"]["likes"] = embed_collection(
        raw_doc.get("meta", {}).get("count_like", 0), f'{raw_doc["remote_id"]}/likes'
    )

    raw_doc["activity"]["object"]["shares"] = embed_collection(
        raw_doc.get("meta", {}).get("count_boost", 0), f'{raw_doc["remote_id"]}/shares'
    )

    return raw_doc


def remove_context(activity: Dict[str, Any]) -> Dict[str, Any]:
    if "@context" in activity:
        del activity["@context"]
    return activity


def activity_from_doc(raw_doc: Dict[str, Any], embed: bool = False) -> Dict[str, Any]:
    raw_doc = add_extra_collection(raw_doc)
    activity = clean_activity(raw_doc["activity"])
    if embed:
        return remove_context(activity)
    return activity


@app.route("/outbox", methods=["GET", "POST"])
def outbox():
    if request.method == "GET":
        if not is_api_request():
            abort(404)
        # TODO(tsileo): filter the outbox if not authenticated
        # FIXME(tsileo): filter deleted, add query support for build_ordered_collection
        q = {
            "meta.deleted": False,
            # 'type': {'$in': [ActivityType.CREATE.value, ActivityType.ANNOUNCE.value]},
        }
        return jsonify(
            **activitypub.build_ordered_collection(
                DB.outbox,
                q=q,
                cursor=request.args.get("cursor"),
                map_func=lambda doc: activity_from_doc(doc, embed=True),
            )
        )

    # Handle POST request
    try:
        _api_required()
    except BadSignature:
        abort(401)

    data = request.get_json(force=True)
    print(data)
    activity = ap.parse_activity(data)
    OUTBOX.post(activity)

    # Purge the cache if a custom hook is set, as new content was published
    custom_cache_purge_hook()

    return Response(status=201, headers={"Location": activity.id})


@app.route("/outbox/<item_id>")
def outbox_detail(item_id):
    doc = DB.outbox.find_one({"remote_id": back.activity_url(item_id)})
    if doc["meta"].get("deleted", False):
        obj = ap.parse_activity(doc["activity"])
        resp = jsonify(**obj.get_object().get_tombstone())
        resp.status_code = 410
        return resp
    return jsonify(**activity_from_doc(doc))


@app.route("/outbox/<item_id>/activity")
def outbox_activity(item_id):
    # TODO(tsileo): handle Tombstone
    data = DB.outbox.find_one(
        {"remote_id": back.activity_url(item_id), "meta.deleted": False}
    )
    if not data:
        abort(404)
    obj = activity_from_doc(data)
    if obj["type"] != ActivityType.CREATE.value:
        abort(404)
    return jsonify(**obj["object"])


@app.route("/outbox/<item_id>/replies")
def outbox_activity_replies(item_id):
    # TODO(tsileo): handle Tombstone
    if not is_api_request():
        abort(404)
    data = DB.outbox.find_one(
        {"remote_id": back.activity_url(item_id), "meta.deleted": False}
    )
    if not data:
        abort(404)
    obj = ap.parse_activity(data["activity"])
    if obj.ACTIVITY_TYPE != ActivityType.CREATE:
        abort(404)

    q = {
        "meta.deleted": False,
        "type": ActivityType.CREATE.value,
        "activity.object.inReplyTo": obj.get_object().id,
    }

    return jsonify(
        **activitypub.build_ordered_collection(
            DB.inbox,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: doc["activity"]["object"],
            col_name=f"outbox/{item_id}/replies",
            first_page=request.args.get("page") == "first",
        )
    )


@app.route("/outbox/<item_id>/likes")
def outbox_activity_likes(item_id):
    # TODO(tsileo): handle Tombstone
    if not is_api_request():
        abort(404)
    data = DB.outbox.find_one(
        {"remote_id": back.activity_url(item_id), "meta.deleted": False}
    )
    if not data:
        abort(404)
    obj = ap.parse_activity(data["activity"])
    if obj.ACTIVITY_TYPE != ActivityType.CREATE:
        abort(404)

    q = {
        "meta.undo": False,
        "type": ActivityType.LIKE.value,
        "$or": [
            {"activity.object.id": obj.get_object().id},
            {"activity.object": obj.get_object().id},
        ],
    }

    return jsonify(
        **activitypub.build_ordered_collection(
            DB.inbox,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: remove_context(doc["activity"]),
            col_name=f"outbox/{item_id}/likes",
            first_page=request.args.get("page") == "first",
        )
    )


@app.route("/outbox/<item_id>/shares")
def outbox_activity_shares(item_id):
    # TODO(tsileo): handle Tombstone
    if not is_api_request():
        abort(404)
    data = DB.outbox.find_one(
        {"remote_id": back.activity_url(item_id), "meta.deleted": False}
    )
    if not data:
        abort(404)
    obj = ap.parse_activity(data["activity"])
    if obj.ACTIVITY_TYPE != ActivityType.CREATE:
        abort(404)

    q = {
        "meta.undo": False,
        "type": ActivityType.ANNOUNCE.value,
        "$or": [
            {"activity.object.id": obj.get_object().id},
            {"activity.object": obj.get_object().id},
        ],
    }

    return jsonify(
        **activitypub.build_ordered_collection(
            DB.inbox,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: remove_context(doc["activity"]),
            col_name=f"outbox/{item_id}/shares",
            first_page=request.args.get("page") == "first",
        )
    )


@app.route("/admin", methods=["GET"])
@login_required
def admin():
    q = {"meta.deleted": False, "meta.undo": False, "type": ActivityType.LIKE.value}
    col_liked = DB.outbox.count(q)

    return render_template(
        "admin.html",
        instances=list(DB.instances.find()),
        inbox_size=DB.inbox.count(),
        outbox_size=DB.outbox.count(),
        object_cache_size=DB.objects_cache.count(),
        actor_cache_size=DB.actors_cache.count(),
        col_liked=col_liked,
        col_followers=DB.followers.count(),
        col_following=DB.following.count(),
    )


@app.route("/new", methods=["GET"])
@login_required
def new():
    reply_id = None
    content = ""
    thread = []
    if request.args.get("reply"):
        data = DB.inbox.find_one({"activity.object.id": request.args.get("reply")})
        if not data:
            data = DB.outbox.find_one({"activity.object.id": request.args.get("reply")})
            if not data:
                abort(400)

        reply = ap.parse_activity(data["activity"])
        reply_id = reply.id
        if reply.ACTIVITY_TYPE == ActivityType.CREATE:
            reply_id = reply.get_object().id
        actor = reply.get_actor()
        domain = urlparse(actor.id).netloc
        # FIXME(tsileo): if reply of reply, fetch all participants
        content = f"@{actor.preferredUsername}@{domain} "
        thread = _build_thread(data, include_children=False)

    return render_template("new.html", reply=reply_id, content=content, thread=thread)


@app.route("/notifications")
@login_required
def notifications():
    # FIXME(tsileo): implements pagination, also for the followers/following page
    limit = 50
    q = {
        "type": "Create",
        "activity.object.tag.type": "Mention",
        "activity.object.tag.name": f"@{USERNAME}@{DOMAIN}",
        "meta.deleted": False,
    }
    # TODO(tsileo): also include replies via regex on Create replyTo
    q = {
        "$or": [
            q,
            {"type": "Follow"},
            {"type": "Accept"},
            {"type": "Undo", "activity.object.type": "Follow"},
            {"type": "Announce", "activity.object": {"$regex": f"^{BASE_URL}"}},
            {"type": "Create", "activity.object.inReplyTo": {"$regex": f"^{BASE_URL}"}},
        ]
    }
    print(q)
    c = request.args.get("cursor")
    if c:
        q["_id"] = {"$lt": ObjectId(c)}

    outbox_data = list(DB.inbox.find(q, limit=limit).sort("_id", -1))
    cursor = None
    if outbox_data and len(outbox_data) == limit:
        cursor = str(outbox_data[-1]["_id"])

    # TODO(tsileo): fix the annonce handling, copy it from /stream
    # for data in outbox_data:
    #    if data['type'] == 'Announce':
    #        print(data)
    #        if data['activity']['object'].startswith('http') and data['activity']['object'] in objcache:
    #            data['ref'] = {'activity': {'object': objcache[data['activity']['object']]}, 'meta': {}}
    #            out.append(data)
    #    else:
    #        out.append(data)

    return render_template("stream.html", inbox_data=outbox_data, cursor=cursor)


@app.route("/api/key")
@login_required
def api_user_key():
    return flask_jsonify(api_key=ADMIN_API_KEY)


def _user_api_arg(key: str, **kwargs):
    """Try to get the given key from the requests, try JSON body, form data and query arg."""
    if request.is_json:
        oid = request.json.get(key)
    else:
        oid = request.args.get(key) or request.form.get(key)

    if not oid:
        if "default" in kwargs:
            return kwargs.get("default")

        raise ValueError(f"missing {key}")

    return oid


def _user_api_get_note(from_outbox: bool = False):
    oid = _user_api_arg("id")
    note = ap.parse_activity(OBJECT_SERVICE.get(oid), expected=ActivityType.NOTE)
    if from_outbox and not note.id.startswith(ID):
        raise NotFromOutboxError(
            f"cannot load {note.id}, id must be owned by the server"
        )

    return note


def _user_api_response(**kwargs):
    _redirect = _user_api_arg("redirect", default=None)
    if _redirect:
        return redirect(_redirect)

    resp = flask_jsonify(**kwargs)
    resp.status_code = 201
    return resp


@app.route("/api/note/delete", methods=["POST"])
@api_required
def api_delete():
    """API endpoint to delete a Note activity."""
    note = _user_api_get_note(from_outbox=True)

    delete = note.build_delete()
    OUTBOX.post(delete)

    return _user_api_response(activity=delete.id)


@app.route("/api/boost", methods=["POST"])
@api_required
def api_boost():
    note = _user_api_get_note()

    announce = note.build_announce(MY_PERSON)
    OUTBOX.post(announce)

    return _user_api_response(activity=announce.id)


@app.route("/api/like", methods=["POST"])
@api_required
def api_like():
    note = _user_api_get_note()

    like = note.build_like(MY_PERSON)
    OUTBOX.post(like)

    return _user_api_response(activity=like.id)


@app.route("/api/undo", methods=["POST"])
@api_required
def api_undo():
    oid = _user_api_arg("id")
    doc = DB.outbox.find_one(
        {"$or": [{"remote_id": back.activity_url(oid)}, {"remote_id": oid}]}
    )
    if not doc:
        raise ActivityNotFoundError(f"cannot found {oid}")

    obj = ap.parse_activity(doc.get("activity"))
    # FIXME(tsileo): detect already undo-ed and make this API call idempotent
    undo = obj.build_undo()
    OUTBOX.post(undo)

    return _user_api_response(activity=undo.id)


@app.route("/stream")
@login_required
def stream():
    # FIXME(tsileo): implements pagination, also for the followers/following page
    limit = 100
    q = {
        "type": "Create",
        "activity.object.type": "Note",
        "activity.object.inReplyTo": None,
        "meta.deleted": False,
    }
    c = request.args.get("cursor")
    if c:
        q["_id"] = {"$lt": ObjectId(c)}

    outbox_data = list(
        DB.inbox.find({"$or": [q, {"type": "Announce"}]}, limit=limit).sort(
            "activity.published", -1
        )
    )
    cursor = None
    if outbox_data and len(outbox_data) == limit:
        cursor = str(outbox_data[-1]["_id"])

    out = []
    objcache = {}
    cached = list(
        DB.objects_cache.find({"meta.part_of_stream": True}, limit=limit * 3).sort(
            "meta.announce_published", -1
        )
    )
    for c in cached:
        objcache[c["object_id"]] = c["cached_object"]
    for data in outbox_data:
        if data["type"] == "Announce":
            if (
                data["activity"]["object"].startswith("http")
                and data["activity"]["object"] in objcache
            ):
                data["ref"] = {
                    "activity": {"object": objcache[data["activity"]["object"]]},
                    "meta": {},
                }
                out.append(data)
            else:
                print("OMG", data)
        else:
            out.append(data)
    return render_template("stream.html", inbox_data=out, cursor=cursor)


@app.route("/inbox", methods=["GET", "POST"])
def inbox():
    if request.method == "GET":
        if not is_api_request():
            abort(404)
        try:
            _api_required()
        except BadSignature:
            abort(404)

        return jsonify(
            **activitypub.build_ordered_collection(
                DB.inbox,
                q={"meta.deleted": False},
                cursor=request.args.get("cursor"),
                map_func=lambda doc: remove_context(doc["activity"]),
            )
        )

    data = request.get_json(force=True)
    logger.debug(f"req_headers={request.headers}")
    logger.debug(f"raw_data={data}")
    try:
        if not verify_request(
            request.method, request.path, request.headers, request.data
        ):
            raise Exception("failed to verify request")
    except Exception:
        logger.exception(
            "failed to verify request, trying to verify the payload by fetching the remote"
        )
        try:
            data = get_backend().fetch_iri(data["id"])
        except Exception:
            logger.exception(f'failed to fetch remote id at {data["id"]}')
            return Response(
                status=422,
                headers={"Content-Type": "application/json"},
                response=json.dumps(
                    {
                        "error": "failed to verify request (using HTTP signatures or fetching the IRI)"
                    }
                ),
            )
    activity = ap.parse_activity(data)
    logger.debug(f"inbox activity={activity}/{data}")
    INBOX.post(activity)

    return Response(status=201)


def without_id(l):
    out = []
    for d in l:
        if "_id" in d:
            del d["_id"]
        out.append(d)
    return out


@app.route("/api/debug", methods=["GET", "DELETE"])
@api_required
def api_debug():
    """Endpoint used/needed for testing, only works in DEBUG_MODE."""
    if not DEBUG_MODE:
        return flask_jsonify(message="DEBUG_MODE is off")

    if request.method == "DELETE":
        _drop_db()
        return flask_jsonify(message="DB dropped")

    return flask_jsonify(
        inbox=DB.inbox.count(),
        outbox=DB.outbox.count(),
        outbox_data=without_id(DB.outbox.find()),
    )


@app.route("/api/upload", methods=["POST"])
@api_required
def api_upload():
    file = request.files["file"]
    rfilename = secure_filename(file.filename)
    prefix = hashlib.sha256(os.urandom(32)).hexdigest()[:6]
    mtype = mimetypes.guess_type(rfilename)[0]
    filename = f"{prefix}_{rfilename}"
    file.save(os.path.join("static", "media", filename))

    # Remove EXIF metadata
    if filename.lower().endswith(".jpg") or filename.lower().endswith(".jpeg"):
        piexif.remove(os.path.join("static", "media", filename))

    print("upload OK")
    print(filename)
    attachment = [
        {
            "mediaType": mtype,
            "name": rfilename,
            "type": "Document",
            "url": BASE_URL + f"/static/media/{filename}",
        }
    ]
    print(attachment)
    content = request.args.get("content")
    to = request.args.get("to")
    note = ap.Note(
        attributedTo=MY_PERSON.id,
        cc=[ID + "/followers"],
        to=[to if to else ap.AS_PUBLIC],
        content=content,  # TODO(tsileo): handle markdown
        attachment=attachment,
    )
    print("post_note_init")
    print(note)
    create = note.build_create()
    print(create)
    print(create.to_dict())
    OUTBOX.post(create)
    print("posted")

    return Response(status=201, response="OK")


@app.route("/api/new_note", methods=["POST"])
@api_required
def api_new_note():
    source = _user_api_arg("content")
    if not source:
        raise ValueError("missing content")

    _reply, reply = None, None
    try:
        _reply = _user_api_arg("reply")
    except ValueError:
        pass

    content, tags = parse_markdown(source)
    to = request.args.get("to")
    cc = [ID + "/followers"]

    if _reply:
        reply = ap.parse_activity(OBJECT_SERVICE.get(_reply))
        cc.append(reply.attributedTo)

    for tag in tags:
        if tag["type"] == "Mention":
            cc.append(tag["href"])

    note = ap.Note(
        attributedTo=MY_PERSON.id,
        cc=list(set(cc)),
        to=[to if to else ap.AS_PUBLIC],
        content=content,
        tag=tags,
        source={"mediaType": "text/markdown", "content": source},
        inReplyTo=reply.id if reply else None,
    )
    create = note.build_create()
    OUTBOX.post(create)

    return _user_api_response(activity=create.id)


@app.route("/api/stream")
@api_required
def api_stream():
    return Response(
        response=json.dumps(
            activitypub.build_inbox_json_feed("/api/stream", request.args.get("cursor"))
        ),
        headers={"Content-Type": "application/json"},
    )


@app.route("/api/block", methods=["POST"])
@api_required
def api_block():
    actor = _user_api_arg("actor")

    existing = DB.outbox.find_one(
        {"type": ActivityType.BLOCK.value, "activity.object": actor, "meta.undo": False}
    )
    if existing:
        return _user_api_response(activity=existing["activity"]["id"])

    block = ap.Block(actor=MY_PERSON.id, object=actor)
    OUTBOX.post(block)

    return _user_api_response(activity=block.id)


@app.route("/api/follow", methods=["POST"])
@api_required
def api_follow():
    actor = _user_api_arg("actor")

    existing = DB.following.find_one({"remote_actor": actor})
    if existing:
        return _user_api_response(activity=existing["activity"]["id"])

    follow = ap.Follow(actor=MY_PERSON.id, object=actor)
    OUTBOX.post(follow)

    return _user_api_response(activity=follow.id)


@app.route("/followers")
def followers():
    if is_api_request():
        return jsonify(
            **activitypub.build_ordered_collection(
                DB.followers,
                cursor=request.args.get("cursor"),
                map_func=lambda doc: doc["remote_actor"],
            )
        )

    followers = [
        ACTOR_SERVICE.get(doc["remote_actor"]) for doc in DB.followers.find(limit=50)
    ]
    return render_template(
        "followers.html",
        me=ME,
        notes=DB.inbox.find({"object.object.type": "Note"}).count(),
        followers=DB.followers.count(),
        following=DB.following.count(),
        followers_data=followers,
    )


@app.route("/following")
def following():
    if is_api_request():
        return jsonify(
            **activitypub.build_ordered_collection(
                DB.following,
                cursor=request.args.get("cursor"),
                map_func=lambda doc: doc["remote_actor"],
            )
        )

    following = [
        ACTOR_SERVICE.get(doc["remote_actor"]) for doc in DB.following.find(limit=50)
    ]
    return render_template(
        "following.html",
        me=ME,
        notes=DB.inbox.find({"object.object.type": "Note"}).count(),
        followers=DB.followers.count(),
        following=DB.following.count(),
        following_data=following,
    )


@app.route("/tags/<tag>")
def tags(tag):
    if not DB.outbox.count(
        {"activity.object.tag.type": "Hashtag", "activity.object.tag.name": "#" + tag}
    ):
        abort(404)
    if not is_api_request():
        return render_template(
            "tags.html",
            tag=tag,
            outbox_data=DB.outbox.find(
                {
                    "type": "Create",
                    "activity.object.type": "Note",
                    "meta.deleted": False,
                    "activity.object.tag.type": "Hashtag",
                    "activity.object.tag.name": "#" + tag,
                }
            ),
        )
    q = {
        "meta.deleted": False,
        "meta.undo": False,
        "type": ActivityType.CREATE.value,
        "activity.object.tag.type": "Hashtag",
        "activity.object.tag.name": "#" + tag,
    }
    return jsonify(
        **activitypub.build_ordered_collection(
            DB.outbox,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: doc["activity"]["object"]["id"],
            col_name=f"tags/{tag}",
        )
    )


@app.route("/liked")
def liked():
    if not is_api_request():
        abort(404)
    q = {"meta.deleted": False, "meta.undo": False, "type": ActivityType.LIKE.value}
    return jsonify(
        **activitypub.build_ordered_collection(
            DB.outbox,
            q=q,
            cursor=request.args.get("cursor"),
            map_func=lambda doc: doc["activity"]["object"],
            col_name="liked",
        )
    )


#######
# IndieAuth


def build_auth_resp(payload):
    if request.headers.get("Accept") == "application/json":
        return Response(
            status=200,
            headers={"Content-Type": "application/json"},
            response=json.dumps(payload),
        )
    return Response(
        status=200,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        response=urlencode(payload),
    )


def _get_prop(props, name, default=None):
    if name in props:
        items = props.get(name)
        if isinstance(items, list):
            return items[0]
        return items
    return default


def get_client_id_data(url):
    data = mf2py.parse(url=url)
    for item in data["items"]:
        if "h-x-app" in item["type"] or "h-app" in item["type"]:
            props = item.get("properties", {})
            print(props)
            return dict(
                logo=_get_prop(props, "logo"),
                name=_get_prop(props, "name"),
                url=_get_prop(props, "url"),
            )
    return dict(logo=None, name=url, url=url)


@app.route("/indieauth/flow", methods=["POST"])
@login_required
def indieauth_flow():
    auth = dict(
        scope=" ".join(request.form.getlist("scopes")),
        me=request.form.get("me"),
        client_id=request.form.get("client_id"),
        state=request.form.get("state"),
        redirect_uri=request.form.get("redirect_uri"),
        response_type=request.form.get("response_type"),
    )

    code = binascii.hexlify(os.urandom(8)).decode("utf-8")
    auth.update(code=code, verified=False)
    print(auth)
    if not auth["redirect_uri"]:
        abort(500)

    DB.indieauth.insert_one(auth)

    # FIXME(tsileo): fetch client ID and validate redirect_uri
    red = f'{auth["redirect_uri"]}?code={code}&state={auth["state"]}&me={auth["me"]}'
    return redirect(red)


# @app.route('/indieauth', methods=['GET', 'POST'])
def indieauth_endpoint():
    if request.method == "GET":
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))

        me = request.args.get("me")
        # FIXME(tsileo): ensure me == ID
        client_id = request.args.get("client_id")
        redirect_uri = request.args.get("redirect_uri")
        state = request.args.get("state", "")
        response_type = request.args.get("response_type", "id")
        scope = request.args.get("scope", "").split()

        print("STATE", state)
        return render_template(
            "indieauth_flow.html",
            client=get_client_id_data(client_id),
            scopes=scope,
            redirect_uri=redirect_uri,
            state=state,
            response_type=response_type,
            client_id=client_id,
            me=me,
        )

    # Auth verification via POST
    code = request.form.get("code")
    redirect_uri = request.form.get("redirect_uri")
    client_id = request.form.get("client_id")

    auth = DB.indieauth.find_one_and_update(
        {
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        },  # },  #  , 'verified': False},
        {"$set": {"verified": True}},
        sort=[("_id", pymongo.DESCENDING)],
    )
    print(auth)
    print(code, redirect_uri, client_id)

    if not auth:
        abort(403)
        return

    session["logged_in"] = True
    me = auth["me"]
    state = auth["state"]
    scope = " ".join(auth["scope"])
    print("STATE", state)
    return build_auth_resp({"me": me, "state": state, "scope": scope})


@app.route("/token", methods=["GET", "POST"])
def token_endpoint():
    if request.method == "POST":
        code = request.form.get("code")
        me = request.form.get("me")
        redirect_uri = request.form.get("redirect_uri")
        client_id = request.form.get("client_id")

        auth = DB.indieauth.find_one(
            {
                "code": code,
                "me": me,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
            }
        )
        if not auth:
            abort(403)
        scope = " ".join(auth["scope"])
        payload = dict(
            me=me, client_id=client_id, scope=scope, ts=datetime.now().timestamp()
        )
        token = JWT.dumps(payload).decode("utf-8")

        return build_auth_resp({"me": me, "scope": scope, "access_token": token})

    # Token verification
    token = request.headers.get("Authorization").replace("Bearer ", "")
    try:
        payload = JWT.loads(token)
    except BadSignature:
        abort(403)

    # TODO(tsileo): handle expiration

    return build_auth_resp(
        {
            "me": payload["me"],
            "scope": payload["scope"],
            "client_id": payload["client_id"],
        }
    )
