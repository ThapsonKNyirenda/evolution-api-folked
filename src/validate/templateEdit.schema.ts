import { JSONSchema7 } from 'json-schema';
import { v4 } from 'uuid';

const isNotEmpty = (...propertyNames: string[]): JSONSchema7 => {
  return {
    allOf: propertyNames.map(property => ({
      if: { required: [property] },
      then: {
        properties: {
          [property]: {
            minLength: 1,
            description: `The "${property}" cannot be empty`,
          },
        },
      },
    })),
  } as JSONSchema7;
};

export const templateEditSchema: JSONSchema7 = {
  $id: v4(),
  type: 'object',
  properties: {
    templateId: { type: 'string' },
    category: { type: 'string', enum: ['AUTHENTICATION', 'MARKETING', 'UTILITY'] },
    allowCategoryChange: { type: 'boolean' },
    ttl: { type: 'number' },
    components: { type: 'array' },
  },
  required: ['templateId'],
  ...isNotEmpty('templateId'),
};
