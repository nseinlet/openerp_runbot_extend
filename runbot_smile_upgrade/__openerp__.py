{
    'name': 'Runbot Smile Upgrade',
    'category': 'Website',
    'summary': 'Runbot',
    'version': '1.0',
    'description': "Runbot can use the smile_upgrade module to upgrade a restored DB",
    'author': 'Odoo SA',
    'depends': ['runbot_restore_db'],
    'data': [
        'runbot.xml',
    ],
    'installable': True,
}
