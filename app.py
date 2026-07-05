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
#  CORS — autoriser toutes les origines
#  (nécessaire pour Capacitor et navigateur)
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

class InventaireCache(db.Model):
    """Cache de l'inventaire du desktop, lisible par le mobile."""
    __tablename__ = 'inventaire_cache'

    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), nullable=False, unique=True)
    produits   = db.Column(db.Text, nullable=False, default='[]')  # JSON
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class ModifMobile(db.Model):
    """Modifications de produits envoyées par le mobile, en attente d'application par le desktop."""
    __tablename__ = 'modifs_mobile'

    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), nullable=False)
    produit_id = db.Column(db.Integer, nullable=False)
    data       = db.Column(db.Text, nullable=False)  # JSON des champs modifiés
    appliquee  = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json
        return {
            'id':         self.id,
            'produit_id': self.produit_id,
            'data':       json.loads(self.data),
        }




# ───────────────────────────────────────────
#  STOCKAGE PRODUITS (cache envoyé par le desktop)
# ───────────────────────────────────────────
class CacheProduits(db.Model):
    __tablename__ = 'cache_produits'

    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), nullable=False, unique=True)
    produits   = db.Column(db.Text, nullable=False)  # JSON
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

# ───────────────────────────────────────────
#  MODIFICATIONS EN ATTENTE (mobile → desktop)
# ───────────────────────────────────────────
class ModifProduit(db.Model):
    __tablename__ = 'modifs_produits'

    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), nullable=False)
    produit_id = db.Column(db.Integer, nullable=False)
    donnees    = db.Column(db.Text, nullable=False)  # JSON
    appliquee  = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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


# ───────────────────────────────────────────
#  POST /api/inventaire/<token> — Desktop pousse son inventaire
# ───────────────────────────────────────────
@app.route('/api/inventaire/<token>', methods=['POST'])
def pousser_inventaire(token):
    import json
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    data = request.get_json() or {}
    produits = data.get('produits', [])

    cache = InventaireCache.query.filter_by(token=token).first()
    if cache:
        cache.produits   = json.dumps(produits)
        cache.updated_at = datetime.utcnow()
    else:
        cache = InventaireCache(token=token, produits=json.dumps(produits))
        db.session.add(cache)

    db.session.commit()
    return jsonify({'message': 'Inventaire mis à jour', 'nb_produits': len(produits)})

# ───────────────────────────────────────────
#  GET /api/inventaire/<token> — Mobile lit l'inventaire
# ───────────────────────────────────────────
@app.route('/api/inventaire/<token>', methods=['GET'])
def lire_inventaire(token):
    import json
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    cache = InventaireCache.query.filter_by(token=token).first()
    if not cache:
        return jsonify([])

    return jsonify(json.loads(cache.produits))

# ───────────────────────────────────────────
#  PUT /api/produits/<token>/<produit_id> — Mobile modifie un produit
# ───────────────────────────────────────────
@app.route('/api/produits/<token>/<int:produit_id>', methods=['PUT'])
def modifier_produit_depuis_mobile(token, produit_id):
    import json
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    data = request.get_json() or {}

    modif = ModifMobile(
        token      = token,
        produit_id = produit_id,
        data       = json.dumps(data),
    )
    db.session.add(modif)
    db.session.commit()

    # Retourner le produit mis à jour depuis le cache
    cache = InventaireCache.query.filter_by(token=token).first()
    if cache:
        produits = json.loads(cache.produits)
        for p in produits:
            if p.get('id') == produit_id:
                p.update(data)
                break
        cache.produits = json.dumps(produits)
        db.session.commit()

        produit_maj = next((p for p in produits if p.get('id') == produit_id), {})
        return jsonify(produit_maj)

    return jsonify(data)

# ───────────────────────────────────────────
#  GET /api/modifs/<token> — Desktop récupère les modifs du mobile
# ───────────────────────────────────────────
@app.route('/api/modifs/<token>', methods=['GET'])
def get_modifs_mobile(token):
    modifs = ModifMobile.query.filter_by(token=token, appliquee=False).all()
    for m in modifs:
        m.appliquee = True
    db.session.commit()
    return jsonify([m.to_dict() for m in modifs])


# ───────────────────────────────────────────
#  POST /api/produits/<token> — Desktop envoie ses produits
# ───────────────────────────────────────────
@app.route('/api/produits/<token>', methods=['POST'])
def pousser_produits(token):
    import json
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    data = request.get_json() or {}
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

# ───────────────────────────────────────────
#  GET /api/produits/<token> — Mobile récupère les produits
# ───────────────────────────────────────────
@app.route('/api/produits/<token>', methods=['GET'])
def get_produits_mobile(token):
    import json
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    cache = CacheProduits.query.filter_by(token=token).first()
    if not cache:
        return jsonify([])

    return jsonify(json.loads(cache.produits))

# ───────────────────────────────────────────
#  PUT /api/produits/<token>/<id> — Mobile modifie un produit
# ───────────────────────────────────────────
@app.route('/api/produits/<token>/<int:produit_id>', methods=['PUT'])
def modifier_produit_depuis_mobile(token, produit_id):
    import json
    pairing = Pairing.query.filter_by(token=token).first()
    if not pairing:
        return jsonify({'message': 'Token introuvable'}), 404

    data = request.get_json() or {}

    modif = ModifProduit(
        token      = token,
        produit_id = produit_id,
        donnees    = json.dumps(data),
    )
    db.session.add(modif)
    db.session.commit()

    # Mettre à jour aussi le cache local
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
#  GET /api/modifs/<token> — Desktop récupère les modifications en attente
# ───────────────────────────────────────────
@app.route('/api/modifs/<token>', methods=['GET'])
def get_modifs(token):
    import json
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