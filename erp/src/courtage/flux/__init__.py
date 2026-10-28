"""Chaine des flux financiers : fichiers recus, controles, couche brute, rapprochement, commissions.

Le module 2 du projet. Il part de fichiers deposes par les compagnies et par la banque, et il produit
trois choses qu'un courtier grossiste delegataire doit savoir produire chaque mois : un etat du
rapprochement entre les cotisations appelees et les cotisations encaissees, le calcul des commissions
dues par les compagnies, et le bordereau de retrocession de chaque apporteur.

L'enchainement est celui du graphe Airflow, et chaque etape est une commande du client en ligne de
commande, utilisable seule :

| Etape | Commande | Ce qu'elle fait |
| --- | --- | --- |
| detection | `courtage flux detecter` | inscrit au journal les fichiers presents dans le dossier d'arrivee |
| archivage | `courtage flux archiver` | calcule l'empreinte SHA-256 et deplace le fichier dans l'archive |
| chargement | `courtage flux charger` | applique les controles, puis charge la couche brute ou rejette le lot entier |
| transformations | `dbt build` | couches staging et intermediaire, et les tests de qualite |
| rapprochement | `courtage flux rapprocher` | rapproche par reference, puis par montant et date |
| commissions | `courtage flux commissions` | commissions et retrocessions au taux valide a l'echeance |
| bordereaux | `courtage flux bordereaux` | bordereaux mensuels, par compagnie et par apporteur |
| publication | `dbt build --select marts` | marts, qui refletent les commissions calculees |
| rapport | `courtage flux rapport` | rappel et precision des anomalies detectees, contre le manifeste |

Le graphe n'importe pas ce paquet : il appelle ces commandes. Airflow et l'ERP n'ont donc pas a partager
leurs dependances, ce qui evite le conflit de versions qui rend tant d'installations d'Airflow fragiles.
"""
