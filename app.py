# =====================================
# app.py — Flask app with Authentication + DeepCNN (corrected inference)
# =====================================
import os
import time
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_mysqldb import MySQL
from werkzeug.security import generate_password_hash, check_password_hash
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image
import io
import os
from dotenv import load_dotenv
from twilio.rest import Client

# Load environment variables
load_dotenv()
# Twilio configuration
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")
USER_PHONE = os.getenv("USER_PHONE")
client = Client(ACCOUNT_SID, AUTH_TOKEN)

# -----------------------------
# Flask + MySQL setup
# -----------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "supersecretkey")

app.config['MYSQL_HOST'] = os.environ.get("MYSQL_HOST", "localhost")
app.config['MYSQL_USER'] = os.environ.get("MYSQL_USER", "root")
app.config['MYSQL_PASSWORD'] = os.environ.get("MYSQL_PASSWORD", "")
app.config['MYSQL_DB'] = os.environ.get("MYSQL_DB", "ransomware")
mysql = MySQL(app)

# -----------------------------
# Device & model path
# -----------------------------
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
MODEL_PATH = os.path.join("model", "ransomware_cnn_model.pth")


def make_alert_call():
    """Trigger a phone call alert when ransomware is detected."""
    try:
        call = client.calls.create(
            twiml='<Response><Say voice="alice">Alert! Alert! Alert! Ransomware has been detected on your system. Please take immediate action.</Say></Response>',
            from_=TWILIO_NUMBER,
            to=USER_PHONE
        )
        print(f"📞 Call initiated successfully. SID: {call.sid}")
    except Exception as e:
        print(f"⚠️ Error making Twilio call: {e}")

# -----------------------------
# DeepCNN (must match training)
# -----------------------------
class DeepCNN(nn.Module):
    def __init__(self):
        super(DeepCNN, self).__init__()
        self.features = nn.Sequential(
            # Block 1
            nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2,2),
            # Block 2
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2,2),
            # Block 3
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(), nn.MaxPool2d(2,2),
            # Block 4
            nn.Conv2d(128, 256, 3, padding=1), nn.BatchNorm2d(256), nn.ReLU(), nn.MaxPool2d(2,2)
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(256 * 8 * 8, 512),
            nn.ReLU(),
            nn.Dropout(0.5),
            nn.Linear(512, 128),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(128, 2)
        )

    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x)
        return x

# instantiate and load model
model = DeepCNN().to(device)

if os.path.exists(MODEL_PATH):
    try:
        state = torch.load(MODEL_PATH, map_location=device)
        model.load_state_dict(state)
        model.eval()
        print(f"✅ Model loaded from {MODEL_PATH} (device={device})")
    except Exception as e:
        print(f"❌ Error loading model: {e}")
else:
    print(f"❌ Model file not found at: {MODEL_PATH}")

# -----------------------------
# Image preprocessing (must match training)
# -----------------------------
transform = transforms.Compose([
    transforms.Grayscale(num_output_channels=1),
    transforms.Resize((128, 128)),
    transforms.ToTensor(),
    transforms.Normalize((0.5,), (0.5,))
])

# -----------------------------
# Auth decorator
# -----------------------------
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return wrapped

# -----------------------------
# Utility: predict single uploaded file
# Accepts a werkzeug FileStorage (request.files['file'])
# -----------------------------
def predict_from_upload(file_storage):
    """
    Returns tuple: (label_str, raw_confidence_float)
    label_str in {"Ransomware", "Legitimate"} or "Error processing image"
    """
    try:
        # Read file bytes -> PIL Image
        raw_bytes = file_storage.read()
        img = Image.open(io.BytesIO(raw_bytes)).convert("L")  # grayscale

        # transforms
        img_t = transform(img).unsqueeze(0).to(device)  # shape [1,1,128,128]

        with torch.no_grad():
            outputs = model(img_t)                       # logits
            probs = torch.softmax(outputs, dim=1)[0]     # [2]
            conf, pred = torch.max(probs, 0)             # conf tensor, pred index
            label = "Ransomware" if pred.item() == 1 else "Legitimate"
            return label, conf.item()
    except Exception as e:
        app.logger.exception("Prediction error")
        return "Error processing image", 0.0

# -----------------------------
# Routes: home, register, login, logout
# -----------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not (username and email and phone and password):
            flash("Please fill all fields.", "danger")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match!", "danger")
            return redirect(url_for("register"))

        hashed = generate_password_hash(password)

        cur = mysql.connection.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                flash("Email already registered.", "warning")
                return redirect(url_for("register"))

            cur.execute(
                "INSERT INTO users (username, email, phone, password) VALUES (%s, %s, %s, %s)",
                (username, email, phone, hashed)
            )
            mysql.connection.commit()
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            mysql.connection.rollback()
            app.logger.exception("DB error during registration")
            flash("Registration failed. Try again.", "danger")
            return redirect(url_for("register"))
        finally:
            cur.close()

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        cur = mysql.connection.cursor()
        try:
            cur.execute("SELECT id, username, email, phone, password FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
        finally:
            cur.close()

        if user and check_password_hash(user[4], password):
            session["logged_in"] = True
            session["user_id"] = user[0]
            session["username"] = user[1]
            session["email"] = user[2]
            session["phone"] = user[3]
            flash("Login successful!", "success")
            next_page = request.args.get("next")
            return redirect(next_page or url_for("predict_page"))
        else:
            flash("Invalid credentials. Please try again.", "danger")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for("index"))

# -----------------------------
# Prediction route (protected)
# -----------------------------
@app.route("/predict", methods=["GET", "POST"])
@login_required
def predict_page():
    result = None
    confidence = None

    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Please upload an image file (grayscale PNG/JPEG).", "warning")
            return redirect(url_for("predict_page"))

        label, conf = predict_from_upload(file)
        confidence = conf
        if "ransomware" in label.lower():
            result = "🚨 Ransomware Detected"
            make_alert_call()  # <-- Call Twilio alert here
            # If you want to trigger Twilio or logging, do it here.
        elif "legitimate" in label.lower():
            result = "✅ Legitimate File"
        else:
            result = f"⚠️ {label}"

    return render_template("predict.html", result=result, confidence=confidence)

# -----------------------------
# Provide auth status to templates
# -----------------------------
@app.context_processor
def inject_auth_status():
    return dict(
        logged_in=session.get("logged_in", False),
        username=session.get("username")
    )

# -----------------------------
# Run app
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True)
