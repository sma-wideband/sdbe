#ifndef VDIF_IN_DATABUF_C
#define VDIF_IN_DATABUF_C

#include "hashpipe.h"
#include "hashpipe_databuf.h"

#include "vdif_in_databuf.h"

hashpipe_databuf_t *vdif_in_databuf_create(int instance_id, int databuf_id)
{
  size_t header_size = sizeof(hashpipe_databuf_t);
  return hashpipe_databuf_create(instance_id, databuf_id, header_size, sizeof(vdif_packet_t), VDIF_IN_BUFFER_SIZE);
}

#endif
