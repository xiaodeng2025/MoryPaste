package cn.shouzhuang.sendtocopyq

import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.TileService

class SendClipboardTile : TileService() {
    override fun onClick() {
        super.onClick()
        if (isLocked) unlockAndRun(::openSender) else openSender()
    }

    @Suppress("DEPRECATION")
    private fun openSender() {
        val intent = Intent(this, SendClipboardActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE))
        } else {
            startActivityAndCollapse(intent)
        }
    }
}
