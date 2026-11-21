#include "AustinDarwinBridge.h"

#include <bsm/libbsm.h>
#include <libproc.h>
#include <string.h>

int32_t AustinCurrentAuditSession(void) {
    auditinfo_addr_t info;
    memset(&info, 0, sizeof(info));
    if (getaudit_addr(&info, sizeof(info)) != 0) {
        return -1;
    }
    return (int32_t)info.ai_asid;
}

bool AustinProcessStartTime(pid_t pid, uint64_t *seconds, uint64_t *microseconds) {
    if (pid <= 0 || seconds == NULL || microseconds == NULL) {
        return false;
    }
    struct proc_bsdinfo info;
    memset(&info, 0, sizeof(info));
    int size = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, (int)sizeof(info));
    if (size != (int)sizeof(info)) {
        return false;
    }
    *seconds = (uint64_t)info.pbi_start_tvsec;
    *microseconds = (uint64_t)info.pbi_start_tvusec;
    return true;
}
