{
    'name': 'Runbot Ps',
    'category': 'Website',
    'summary': 'Runbot',
    'version': '1.0',
    'description': "Runbot",
    'author': 'OpenERP SA',
    'depends': ['runbot'],
    'data': [
        'runbot.xml',
        'security/ir.model.access.csv',
    ],
    'installable': True,
}
