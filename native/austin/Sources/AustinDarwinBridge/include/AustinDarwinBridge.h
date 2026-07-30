#ifndef AUSTIN_DARWIN_BRIDGE_H
#define AUSTIN_DARWIN_BRIDGE_H

#include <stdbool.h>
#include <stdint.h>
#include <sys/types.h>

int32_t AustinCurrentAuditSession(void);
bool AustinProcessStartTime(pid_t pid, uint64_t *seconds, uint64_t *microseconds);

#endif
