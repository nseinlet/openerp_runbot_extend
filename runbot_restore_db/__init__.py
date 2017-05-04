import openerp

#Monkey patch fqdn method
import socket
def fqdn():
    name = socket.getfqdn()
    if name=="api3.odoo.com":
        return "psrunbot.odoo.com"
    elif name=="api4.odoo.com":
        return "psrunbot2.odoo.com"
    return name
    
openerp.addons.runbot.runbot.fqdn = fqdn

import runbot
