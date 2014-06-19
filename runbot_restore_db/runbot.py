from openerp.osv import fields, osv

class runbot_repo(osv.Model):
    _inherit = "runbot.repo"

    _columns = {
        'db_name': fields.char("Database name to replicate"),
        'no_build': fields.boolean('Do not buid'),
    }

    _defaults = {
        'no_build': False,
    }

    def update_git(self, cr, uid, repo, context=None):
        super(runbot_repo, self).update_git(cr, uid, repo, context)
        bds = self.pool['runbot.build']
        bds_ids = bds.search(cr, uid, [('repo_id', '=', repo.id), ('state', '=', 'pending')], context=context)
        bds.write(cr, uid, bds_ids, {'state': 'done'}, context=context)

class runbot_build(osv.osv):
    _inherit = "runbot.build"

    def job_25_restore(self, cr, uid, build, lock_path, log_path):
        if not build.repo_id.db_name:
            return 0
        self.pg_createdb(cr, uid, "%s-all" % build.dest)
        cmd = "pg_dump %s | psql %s-all" % (build.repo_id.db_name, build.dest)
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None, shell=True)

    def job_26_upgrade(self, cr, uid, build, lock_path, log_path):
        if not build.repo_id.db_name:
            return 0
        cmd, mods = build.cmd()
        cmd += ['-d', '%s-all' % build.dest, '-u', 'all', '--stop-after-init', '--log-level=debug']
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)
