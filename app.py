# ═══════════════════════════════════════════════════════════
#  FLUXIA RELAY — app.py
#  Serveur intermédiaire entre Desktop et Mobile
# ═══════════════════════════════════════════════════════════

import os
import json
import secrets
from datetime import datetime
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
import firebase_admin
from firebase_admin import credentials, messaging

app = Flask(__name__)

# ───────────────────────────────────────────
#  FIREBASE ADMIN SDK
# ───────────────────────────────────────────
firebase_pret = False
try:
    firebase_creds_json = os.environ.get('FIREBASE_CREDENTIALS')
    if firebase_creds_json:
        cred = credentials.Certificate(json.loads(firebase_creds_json))
        firebase_admin.initialize_app(cred)
        firebase_pret = True
        print('[Relay] Firebase Admin initialisé ✓')
    else:
        print('[Relay] FIREBASE_CREDENTIALS absent — notifications push désactivées')
except Exception as e:
    print(f'[Relay] Erreur initialisation Firebase: {e}')

# ───────────────────────────────────────────
#  CORS
# ───────────────────────────────────────────
@app.after_request
def ajouter_cors(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return jsonify({}), 200

# ───────────────────────────────────────────
#  BASE DE DONNÉES
# ───────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///relay.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI']        = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ───────────────────────────────────────────
#  MODÈLES
# ───────────────────────────────────────────
class Pairing(db.Model):
    __tablename__ = 'pairings'
    id           = db.Column(db.Integer, primary_key=True)
    token        = db.Column(db.String(64), unique=True, nullable=False)
    mobile_id    = db.Column(db.String(100), nullable=True)
    boutique_nom = db.Column(db.String(200), default='Ma Boutique')
    connecte     = db.Column(db.Boolean, default=False)
    fcm_token    = db.Column(db.String(300), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'token':        self.token,
            'connecte':     self.connecte,
            'boutique_nom': self.boutique_nom,
        }


class Message(db.Model):
    __tablename__ = 'messages'
    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), nullable=False)
    type       = db.Column(db.String(30), nullable=False)
    titre      = db.Column(db.String(150), nullable=False)
    contenu    = db.Column(db.Text, nullable=False)
    livre      = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':      self.id,
            'type':    self.type,
            'titre':   self.titre,
            'contenu': json.loads(self.contenu),
            'date':    self.created_at.isoformat(),
        }


class CacheProduits(db.Model):
    __tablename__ = 'cache_produits'
    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), nullable=False, unique=True)
    produits   = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class ModifProduit(db.Model):
    __tablename__ = 'modifs_produits'
    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), nullable=False)
    produit_id = db.Column(db.Integer, nullable=False)
    donnees    = db.Column(db.Text, nullable=False)
    appliquee  = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()

    # Migration légère : ajoute la colonne fcm_token si elle n'existe pas déjà
    try:
        db.session.execute(db.text('ALTER TABLE pairings ADD COLUMN fcm_token VARCHAR(300)'))
        db.session.commit()
        print('[Relay] Colonne fcm_token ajoutée ✓')
    except Exception:
        db.session.rollback()
        # La colonne existe déjà (ou autre souci mineur) — on ignore silencieusement

# ───────────────────────────────────────────
#  ROUTE DE TEST
# ───────────────────────────────────────────
@app.route('/')
def accueil():
    return jsonify({'service': 'Fluxia Relay', 'statut': 'actif', 'version': '1.0.0'})

# ───────────────────────────────────────────
#  PAIRING
# ───────────────────────────────────────────
@app.route('/api/pairing/generer', methods=['POST'])
def generer_pairing():
    data         = request.get_json() or {}
    boutique_nom = data.get('boutique_nom', 'Ma Boutique')
    token        = secrets.token_urlsafe(24)
    pairing      = Pairing(token=token, boutique_nom=boutique_nom)
    db.session.add(pairing)
    db.session.commit()
    return jsonify({'token': token}), 201


@app.route('/api/pairing/scanner', methods=['POST'])
def scanner_pairing():
    data      = request.get_json() or {}
    token     = data.get('token')
    mobile_id = data.get('mobile_id')
    if not token or not mobile_id:
        return jsonify({'message': 'Token et mobile_id requis'}), 400
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Code invalide ou expiré'}), 404
    pairing.mobile_id = mobile_id
    pairing.connecte  = True
    db.session.commit()
    return jsonify(pairing.to_dict())


@app.route('/api/pairing/<token>/statut', methods=['GET'])
def statut_pairing(token):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404
    return jsonify(pairing.to_dict())

# ───────────────────────────────────────────
#  MESSAGES
# ───────────────────────────────────────────
@app.route('/api/messages/<token>', methods=['POST'])
def envoyer_message(token):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404
    data    = request.get_json() or {}
    message = Message(
        token   = token,
        type    = data.get('type', 'info'),
        titre   = data.get('titre', 'Notification'),
        contenu = json.dumps(data.get('contenu', {})),
    )
    db.session.add(message)
    db.session.commit()

    # Envoyer un vrai push FCM en plus (si le mobile a un token enregistré)
    envoyer_push_fcm(pairing, data.get('titre', 'Notification'), data.get('type', 'info'), data.get('contenu', {}))

    return jsonify(message.to_dict()), 201


def envoyer_push_fcm(pairing, titre, type_event, contenu):
    if not firebase_pret or not pairing.fcm_token:
        return

    # Construire un corps de notification lisible selon le type
    if type_event == 'vente':
        corps = f"{contenu.get('nb_articles', 0)} article(s) — {contenu.get('montant', 0)} KMF"
    elif type_event == 'cloture':
        corps = f"CA : {contenu.get('total_ca', 0)} KMF — {contenu.get('nb_transactions', 0)} ventes"
    elif type_event == 'stock_faible':
        corps = contenu.get('message', '')
    else:
        corps = ''

    try:
        message = messaging.Message(
            notification=messaging.Notification(title=titre, body=corps),
            data={'type': type_event},
            token=pairing.fcm_token,
        )
        messaging.send(message)
    except Exception as e:
        print(f'[Relay] Échec envoi push FCM: {e}')


@app.route('/api/fcm-token/<token>', methods=['POST'])
def enregistrer_fcm_token(token):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    data      = request.get_json() or {}
    fcm_token = data.get('fcm_token')
    if not fcm_token:
        return jsonify({'message': 'fcm_token requis'}), 400

    pairing.fcm_token = fcm_token
    db.session.commit()
    return jsonify({'message': 'Token FCM enregistré'})


@app.route('/api/messages/<token>', methods=['GET'])
def recuperer_messages(token):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404
    messages = Message.query.filter_by(token=token, livre=False).order_by(Message.created_at).all()
    for m in messages:
        m.livre = True
    db.session.commit()
    return jsonify([m.to_dict() for m in messages])

# ───────────────────────────────────────────
#  PRODUITS
# ───────────────────────────────────────────
@app.route('/api/produits/<token>', methods=['POST'])
def pousser_produits(token):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404
    data          = request.get_json() or {}
    produits_json = json.dumps(data.get('produits', []))
    cache = CacheProduits.query.filter_by(token=token).first()
    if cache:
        cache.produits   = produits_json
        cache.updated_at = datetime.utcnow()
    else:
        cache = CacheProduits(token=token, produits=produits_json)
        db.session.add(cache)
    db.session.commit()
    return jsonify({'message': 'Produits mis à jour'})


@app.route('/api/produits/<token>', methods=['GET'])
def get_produits_relay(token):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404
    cache = CacheProduits.query.filter_by(token=token).first()
    if not cache:
        return jsonify([])
    return jsonify(json.loads(cache.produits))


@app.route('/api/produits/<token>/<int:produit_id>', methods=['PUT'])
def modifier_produit_relay(token, produit_id):
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404
    data  = request.get_json() or {}
    modif = ModifProduit(
        token      = token,
        produit_id = produit_id,
        donnees    = json.dumps(data),
    )
    db.session.add(modif)
    # Mettre à jour le cache local
    cache = CacheProduits.query.filter_by(token=token).first()
    if cache:
        produits = json.loads(cache.produits)
        for p in produits:
            if p.get('id') == produit_id:
                p.update(data)
                break
        cache.produits   = json.dumps(produits)
        cache.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'message': 'Modification enregistrée', 'produit_id': produit_id})

# ───────────────────────────────────────────
#  MODIFICATIONS EN ATTENTE (desktop récupère)
# ───────────────────────────────────────────
@app.route('/api/modifs/<token>', methods=['GET'])
def get_modifs(token):
    modifs = ModifProduit.query.filter_by(token=token, appliquee=False).all()
    result = []
    for m in modifs:
        result.append({
            'id':         m.id,
            'produit_id': m.produit_id,
            'donnees':    json.loads(m.donnees),
        })
        m.appliquee = True
    db.session.commit()
    return jsonify(result)

# ───────────────────────────────────────────
#  LANCEMENT
# ───────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=True)