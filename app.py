# ═══════════════════════════════════════════════════════════
#  FLUXIA RELAY — app.py
#  Petit serveur intermédiaire entre Desktop et Mobile
#  V1 : pairing par token + file d'attente par polling
# ═══════════════════════════════════════════════════════════

import os
import secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# ───────────────────────────────────────────
#  CONFIGURATION BASE DE DONNÉES
# ───────────────────────────────────────────
# Render fournit une variable DATABASE_URL en production.
# En local (test), on utilise SQLite par simplicité.
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///relay.db')

# Render donne parfois des URLs commençant par postgres:// au lieu
# de postgresql://, SQLAlchemy moderne a besoin de postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI']        = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ───────────────────────────────────────────
#  MODÈLES
# ───────────────────────────────────────────

class Pairing(db.Model):
    """Lie un desktop (via token) à un mobile (via device_id)."""
    __tablename__ = 'pairings'

    id          = db.Column(db.Integer, primary_key=True)
    token       = db.Column(db.String(64), unique=True, nullable=False)  # Généré par le desktop
    mobile_id   = db.Column(db.String(100), nullable=True)               # Rempli quand le mobile scanne
    boutique_nom= db.Column(db.String(200), default='Ma Boutique')
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    connecte    = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'token':        self.token,
            'connecte':     self.connecte,
            'boutique_nom': self.boutique_nom,
        }


class Message(db.Model):
    """File d'attente des événements (vente, clôture, etc.) à livrer au mobile."""
    __tablename__ = 'messages'

    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), nullable=False)  # À quel pairing ça appartient
    type       = db.Column(db.String(30), nullable=False)  # 'vente' | 'cloture' | 'stock_faible'
    titre      = db.Column(db.String(150), nullable=False)
    contenu    = db.Column(db.Text, nullable=False)         # JSON sous forme de texte
    livre      = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json
        return {
            'id':      self.id,
            'type':    self.type,
            'titre':   self.titre,
            'contenu': json.loads(self.contenu),
            'date':    self.created_at.isoformat(),
        }

# Créer les tables au démarrage
with app.app_context():
    db.create_all()

# ───────────────────────────────────────────
#  ROUTE DE TEST — pour vérifier que le serveur tourne
# ───────────────────────────────────────────
@app.route('/')
def accueil():
    return jsonify({
        'service': 'Fluxia Relay',
        'statut':  'actif',
        'version': '1.0.0',
    })

# ───────────────────────────────────────────
#  POST /api/pairing/generer — Desktop génère un nouveau token
# ───────────────────────────────────────────
@app.route('/api/pairing/generer', methods=['POST'])
def generer_pairing():
    data = request.get_json() or {}
    boutique_nom = data.get('boutique_nom', 'Ma Boutique')

    token = secrets.token_urlsafe(24)  # Token unique et sûr

    pairing = Pairing(token=token, boutique_nom=boutique_nom)
    db.session.add(pairing)
    db.session.commit()

    return jsonify({'token': token}), 201

# ───────────────────────────────────────────
#  POST /api/pairing/scanner — Mobile scanne le QR code
# ───────────────────────────────────────────
@app.route('/api/pairing/scanner', methods=['POST'])
def scanner_pairing():
    data       = request.get_json() or {}
    token      = data.get('token')
    mobile_id  = data.get('mobile_id')

    if not token or not mobile_id:
        return jsonify({'message': 'Token et mobile_id requis'}), 400

    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Code invalide ou expiré'}), 404

    pairing.mobile_id = mobile_id
    pairing.connecte  = True
    db.session.commit()

    return jsonify(pairing.to_dict())

# ───────────────────────────────────────────
#  GET /api/pairing/<token>/statut — Desktop vérifie si connecté
# ───────────────────────────────────────────
@app.route('/api/pairing/<token>/statut', methods=['GET'])
def statut_pairing(token):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404
    return jsonify(pairing.to_dict())

# ───────────────────────────────────────────
#  POST /api/messages/<token> — Desktop envoie un événement
# ───────────────────────────────────────────
@app.route('/api/messages/<token>', methods=['POST'])
def envoyer_message(token):
    import json

    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    data = request.get_json() or {}

    message = Message(
        token   = token,
        type    = data.get('type', 'info'),
        titre   = data.get('titre', 'Notification'),
        contenu = json.dumps(data.get('contenu', {})),
    )
    db.session.add(message)
    db.session.commit()

    return jsonify(message.to_dict()), 201

# ───────────────────────────────────────────
#  GET /api/messages/<token> — Mobile récupère ses messages non livrés
# ───────────────────────────────────────────
@app.route('/api/messages/<token>', methods=['GET'])
def recuperer_messages(token):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    messages = Message.query.filter_by(token=token, livre=False).order_by(Message.created_at).all()

    # Marquer comme livrés une fois récupérés
    for m in messages:
        m.livre = True
    db.session.commit()

    return jsonify([m.to_dict() for m in messages])

# ───────────────────────────────────────────
#  LANCEMENT (développement local uniquement)
# ───────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=True)