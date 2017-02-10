# -*- encoding: utf-8 -*-
from openerp import models, fields, api, exceptions, _

class RunbotRepo(models.Model):
    _inherit = 'runbot.repo'
    
    use_smile_upgrade = fields.Boolean("Use smile upgrade module")

    
class RunbotRepo(models.Model):
    _inherit = 'runbot.build'
    
    def job_26_upgrade(self, cr, uid, build, lock_path, log_path):
        if build.repo_id.use_smile_upgrade:
            with open("%s/build.cfg" % build.path(), 'w') as cfg:
                cfg.write("[options]\n")
                cfg.write("upgrades_path = %s\n" % build.path())
                cfg.write("stop_after_upgrades = False\n")
            if not build.repo_id.db_name:
                return 0
            to_test = build.modules if build.modules and not build.repo_id.force_update_all else 'all'
            cmd, mods = build.cmd()
            cmd += ['-c', 'build.cfg', '-d', '%s-all' % build.dest, '-u', to_test, '--stop-after-init', '--log-level=info']
            if not build.repo_id.no_testenable_job26:
                cmd.append("--test-enable")
            return self.spawn(cmd, lock_path, log_path, cpu_limit=None)
        else:
            return super(RunbotRepo, self).job_26_upgrade(cr, uid, build, lock_path, log_path)
